"""Inserting a library asset into a post — a slide, or a Reel.

Copies bytes rather than referencing the library row: deleting the asset
afterwards must leave the post's own copy standing, which is the whole
justification for not simply pointing Slide.image_path at the library file
(see MediaAsset's docstring in models/database.py). That is the one test here
that matters most.
"""
from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db
from main import app
from models.database import Base, MediaAsset, Post as PostModel, Slide as SlideModel
from services import media_store

POSTS_DIR = Path(__file__).resolve().parents[1] / "uploads" / "posts"


def _jpeg(color: str = "blue") -> bytes:
    img = Image.new("RGB", (100, 100), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(media_store, "MEDIA_ROOT", tmp_path / "media")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")

    async def _ensure():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_ensure())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.state.sessionmaker = SM
    tc = TestClient(app)
    yield tc
    app.dependency_overrides.pop(get_db, None)
    asyncio.run(eng.dispose())


def _seed_post_with_slide(post_id: str, slide_num: int = 1) -> Path:
    """Local (desktop) mode owns everything, so no user_id is needed on the
    row — matching test_slide_replace.py's own fixture."""
    async def _go():
        async with app.state.sessionmaker() as s:
            s.add(PostModel(id=post_id, topic="t", format="single", status="preview",
                            caption="c", hashtags=[], platform="instagram"))
            s.add(SlideModel(post_id=post_id, slide_number=slide_num, image_source="stock",
                             image_path=str(POSTS_DIR / post_id / f"slide_{slide_num}.jpg")))
            await s.commit()
    asyncio.run(_go())
    path = POSTS_DIR / post_id / f"slide_{slide_num}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_jpeg("red"))
    return path


def _seed_asset(asset_id: str, kind: str = "image", status: str = "ready",
                data: bytes | None = None, mime: str = "image/jpeg",
                user_id: str = "someone") -> None:
    async def _go():
        async with app.state.sessionmaker() as s:
            s.add(MediaAsset(id=asset_id, user_id=user_id, kind=kind,
                             source="upload", status=status, mime=mime))
            await s.commit()
    asyncio.run(_go())
    if data is not None:
        media_store.save(user_id, asset_id, data, mime)


def _new_id() -> str:
    return str(uuid.uuid4())


# ------------------------------------------------------------------ slide from-library

def test_slide_from_library_replaces_the_image(client):
    post_id = _new_id()
    path = _seed_post_with_slide(post_id)
    old_bytes = path.read_bytes()
    asset_id = _new_id()
    _seed_asset(asset_id, data=_jpeg("green"))

    r = client.post(f"/api/posts/{post_id}/slides/1/from-library",
                    json={"asset_id": asset_id})
    assert r.status_code == 200
    body = r.json()
    assert body["image_source"] == "upload"
    assert path.read_bytes() != old_bytes


def test_slide_from_library_404_for_unknown_post(client):
    asset_id = _new_id()
    _seed_asset(asset_id, data=_jpeg())
    r = client.post(f"/api/posts/{_new_id()}/slides/1/from-library",
                    json={"asset_id": asset_id})
    assert r.status_code == 404


def test_slide_from_library_404_for_unknown_slide(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id, slide_num=1)
    asset_id = _new_id()
    _seed_asset(asset_id, data=_jpeg())
    r = client.post(f"/api/posts/{post_id}/slides/9/from-library",
                    json={"asset_id": asset_id})
    assert r.status_code == 404


def test_slide_from_library_404_for_unknown_asset(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    r = client.post(f"/api/posts/{post_id}/slides/1/from-library",
                    json={"asset_id": _new_id()})
    assert r.status_code == 404


def test_slide_from_library_refuses_a_video_asset(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, kind="video", data=b"mp4-bytes", mime="video/mp4")
    r = client.post(f"/api/posts/{post_id}/slides/1/from-library",
                    json={"asset_id": asset_id})
    assert r.status_code == 400


def test_slide_from_library_refuses_a_pending_asset(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, status="pending")
    r = client.post(f"/api/posts/{post_id}/slides/1/from-library",
                    json={"asset_id": asset_id})
    assert r.status_code == 400


def test_deleting_the_asset_afterwards_leaves_the_slide_intact(client):
    """The whole justification for copying rather than referencing: the post
    must not go blank when the library row it came from is gone."""
    post_id = _new_id()
    path = _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, data=_jpeg("green"))

    client.post(f"/api/posts/{post_id}/slides/1/from-library", json={"asset_id": asset_id})
    copied_bytes = path.read_bytes()

    media_store.delete("someone", asset_id)
    assert path.exists()
    assert path.read_bytes() == copied_bytes


# ------------------------------------------------------------------ reel from-library

def test_reel_from_library_sets_the_video_and_is_servable(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, kind="video", data=b"mp4-bytes-here", mime="video/mp4")

    r = client.put(f"/api/posts/{post_id}/reel/from-library", json={"asset_id": asset_id})
    assert r.status_code == 200
    assert r.json()["size_bytes"] == len(b"mp4-bytes-here")

    served = client.get(r.json()["video_url"])
    assert served.status_code == 200
    assert served.content == b"mp4-bytes-here"


def test_reel_from_library_refuses_an_image_asset(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, data=_jpeg())
    r = client.put(f"/api/posts/{post_id}/reel/from-library", json={"asset_id": asset_id})
    assert r.status_code == 400


def test_reel_from_library_refuses_a_pending_asset(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, kind="video", status="pending")
    r = client.put(f"/api/posts/{post_id}/reel/from-library", json={"asset_id": asset_id})
    assert r.status_code == 400


def test_reel_from_library_404_for_unknown_post(client):
    asset_id = _new_id()
    _seed_asset(asset_id, kind="video", data=b"x", mime="video/mp4")
    r = client.put(f"/api/posts/{_new_id()}/reel/from-library", json={"asset_id": asset_id})
    assert r.status_code == 404


def test_deleting_the_video_asset_afterwards_leaves_the_reel_playable(client):
    post_id = _new_id()
    _seed_post_with_slide(post_id)
    asset_id = _new_id()
    _seed_asset(asset_id, kind="video", data=b"mp4-bytes-here", mime="video/mp4")
    r = client.put(f"/api/posts/{post_id}/reel/from-library", json={"asset_id": asset_id})

    media_store.delete("someone", asset_id)
    served = client.get(r.json()["video_url"])
    assert served.status_code == 200
    assert served.content == b"mp4-bytes-here"
