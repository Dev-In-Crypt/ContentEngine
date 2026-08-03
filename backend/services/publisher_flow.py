"""Shared publish pipeline used by both immediate publish and scheduled jobs.

Flow: load post → read slide JPEGs → hand them to the platform's Publisher
(Instagram, X, …) → record the published id / permalink, or mark the post failed.
The platform-specific upload+publish lives in services/publishing/*.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from models.database import Post as PostModel, User as UserModel
from models.schemas import TWEET_CHAR_LIMIT
from services.instagram import InstagramPublisher
from services.publishing.factory import make_publisher_for
from services.user_settings import settings_for_post_owner
from services.x_text import append_tags


class PublishError(Exception):
    pass


def build_x_text(post: PostModel, owner: Optional[UserModel]) -> tuple[str, list[str], bool]:
    """(caption, thread_parts, long_form) for an X post.

    Tags ride the last tweet of a thread and the end of a single tweet;
    long-form (skipping the char cap) is keyed off the OWNER's x_premium flag,
    not length alone — a non-Premium caption that happens to be long must
    still be cut, not sent uncut and rejected by X. One place, because a
    photo/text publish (publish_now, below) and a video publish (Phase 8) must
    not drift apart on this.
    """
    tags = " ".join(post.hashtags or []).strip()
    body = (post.caption or "").strip()
    thread_parts = list(post.thread_parts or [])
    owner_premium = bool(owner and getattr(owner, "x_premium", False))
    long_form = not thread_parts and len(body) > TWEET_CHAR_LIMIT and owner_premium
    caption = append_tags(body, tags, limit=None if long_form else TWEET_CHAR_LIMIT)
    if thread_parts:
        thread_parts[-1] = append_tags(thread_parts[-1], tags)
    return caption, thread_parts, long_form


async def publish_now(sessionmaker, post_id: str) -> str:
    """Publish a post to its platform immediately. Returns the platform post id.

    Raises PublishError on any failure (and marks the post as failed in DB).
    `sessionmaker` is an async_sessionmaker (app.state.sessionmaker).
    """
    async with sessionmaker() as db:
        # with_for_update locks the post row for this transaction on Postgres, so a
        # concurrent manual publish and scheduled job can't both pass the
        # already-published check below and double-post. No-op on SQLite (single
        # writer anyway), which keeps the tests unchanged.
        result = await db.execute(
            select(PostModel).where(PostModel.id == post_id)
            .options(selectinload(PostModel.slides))
            .with_for_update()
        )
        post = result.scalar_one_or_none()
        if not post:
            raise PublishError(f"Post {post_id} not found")

        # Idempotency: if it's already live, return the existing id instead of
        # publishing a second time. Covers the double-click and the race where a
        # manual publish and the scheduled job both fire.
        if post.status == "published" and post.instagram_media_id:
            return post.instagram_media_id

        # Publish with the POST OWNER's own keys (multi-tenant), falling back to
        # the platform .env for the local desktop user / unowned posts.
        settings = await settings_for_post_owner(db, post)

        # The factory gates credentials for the post's platform and raises
        # PublishError if they're missing.
        platform = post.platform or "instagram"
        try:
            publisher = make_publisher_for(platform, settings, name_prefix=post.id[:8])
        except Exception as e:
            raise PublishError(str(e)) from e

        # Read slide images from disk in slide order.
        slides = sorted(post.slides, key=lambda s: s.slide_number)
        images: list[bytes] = []
        for s in slides:
            p = Path(s.image_path) if s.image_path else None
            if not p or not p.exists():
                await _mark_failed(db, post, f"Image missing for slide {s.slide_number}")
                raise PublishError(f"Image missing for slide {s.slide_number}")
            images.append(p.read_bytes())
        # X can post pure text (a tweet needs no media); every other platform
        # requires an image, so a zero-slide post there is an error.
        if not images and platform != "x":
            await _mark_failed(db, post, "No slides to publish")
            raise PublishError("No slides to publish")

        tags = " ".join(post.hashtags or []).strip()
        body = (post.caption or "").strip()

        if platform == "x":
            owner = await db.get(UserModel, post.user_id) if post.user_id else None
            caption, thread_parts, long_form = build_x_text(post, owner)
        else:
            caption = f"{body}\n\n{tags}".strip()
            thread_parts = list(post.thread_parts or [])
            long_form = False

        try:
            if thread_parts and platform == "x":
                outcome = await publisher.publish_thread(
                    thread_parts, images, post.alt_text or "")
            elif platform == "x":
                outcome = await publisher.publish(
                    images, caption, post.alt_text or "", long_form=long_form)
            else:
                outcome = await publisher.publish(images, caption, post.alt_text or "")
        except Exception as e:
            # Any failure (upload, platform API, timeout, network) marks the post
            # failed so it never sits stuck 'scheduled'.
            await _mark_failed(db, post, str(e))
            raise PublishError(str(e)) from e
        finally:
            await publisher.close()

        post.status = "published"
        post.instagram_media_id = outcome.media_id   # platform post id (name kept for back-compat)
        post.published_url = outcome.permalink
        post.published_at = datetime.now(timezone.utc)
        post.published_image_urls = outcome.image_urls
        post.schedule_error = None
        await db.commit()
        return outcome.media_id


async def publish_reel_now(sessionmaker, post_id: str, video_url: str) -> str:
    """Publish an already-rendered Reel MP4 (served at `video_url`) to Instagram.

    `video_url` must be publicly reachable by Instagram — in cloud mode this is
    PUBLIC_BASE_URL + /api/posts/{id}/reel/video. Raises PublishError on failure.
    """
    async with sessionmaker() as db:
        # Lock the row (Postgres) so a manual + scheduled reel publish can't race
        # past the already-published check. No-op on SQLite.
        result = await db.execute(
            select(PostModel).where(PostModel.id == post_id).with_for_update()
        )
        post = result.scalar_one_or_none()
        if not post:
            raise PublishError(f"Post {post_id} not found")

        # Instagram pulls the MP4 from a public URL; X takes the bytes directly
        # and has its own asynchronous path (services/x_video_publish.py, via
        # POST /{post_id}/publish-video). This function stays the Instagram half.
        if (post.platform or "instagram") != "instagram":
            raise PublishError(
                "This is an X post — publish its video with Publish to X, "
                "not the Instagram reel path.")

        # Idempotency: already live → return the existing media id (mirrors
        # publish_now; guards double-click and manual+job races).
        if post.status == "published" and post.instagram_media_id:
            return post.instagram_media_id

        # Publish with the post owner's Instagram credentials (platform .env for
        # the local desktop user / unowned posts).
        settings = await settings_for_post_owner(db, post)
        if not settings.instagram_access_token or not settings.instagram_user_id:
            raise PublishError("Instagram credentials not configured")

        caption = f"{post.caption or ''}\n\n{' '.join(post.hashtags or [])}".strip()
        publisher = InstagramPublisher(
            access_token=settings.instagram_access_token,
            ig_user_id=settings.instagram_user_id,
        )
        try:
            media_id = await publisher.publish_reel(video_url=video_url, caption=caption)
        except Exception as e:
            # Any failure (IG, timeout, network) marks the post failed so it never
            # sits stuck in its prior status.
            await _mark_failed(db, post, str(e))
            raise PublishError(str(e)) from e
        finally:
            await publisher.close()

        post.status = "published"
        post.instagram_media_id = media_id
        post.published_at = datetime.now(timezone.utc)
        post.schedule_error = None
        await db.commit()
        return media_id


async def _mark_failed(db, post: PostModel, error: str) -> None:
    post.status = "failed"
    post.schedule_error = error[:1000]
    await db.commit()
