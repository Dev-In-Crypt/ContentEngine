"""Disk cleanup: orphaned upload dirs are removed, live posts' files are kept."""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import Base, MediaAsset, Post
from services import media_store
from services.cleanup import (
    cleanup_orphaned_uploads, find_orphaned_dirs, run_upload_cleanup,
)


def _make_post_dir(root, post_id, files=("slide_1.jpg",)):
    d = root / post_id
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_bytes(b"x" * 100)
    return d


def test_find_orphaned_dirs(tmp_path):
    _make_post_dir(tmp_path, "live-1")
    _make_post_dir(tmp_path, "orphan-1")
    _make_post_dir(tmp_path, "orphan-2")
    orphans = {d.name for d in find_orphaned_dirs(tmp_path, {"live-1"})}
    assert orphans == {"orphan-1", "orphan-2"}


def test_find_orphaned_dirs_missing_root(tmp_path):
    assert find_orphaned_dirs(tmp_path / "nope", {"x"}) == []


def test_cleanup_removes_orphans_keeps_live(tmp_path):
    live = _make_post_dir(tmp_path, "live-1", ("slide_1.jpg", "slide_1_raw.jpg"))
    orphan = _make_post_dir(tmp_path, "orphan-1", ("reel.mp4",))
    res = cleanup_orphaned_uploads(tmp_path, {"live-1"})
    assert res["removed"] == 1
    assert res["freed_bytes"] == 100        # one 100-byte file in the orphan
    assert live.exists() and (live / "slide_1_raw.jpg").exists()   # live untouched
    assert not orphan.exists()


def test_cleanup_noop_when_all_live(tmp_path):
    _make_post_dir(tmp_path, "a")
    _make_post_dir(tmp_path, "b")
    res = cleanup_orphaned_uploads(tmp_path, {"a", "b"})
    assert res == {"removed": 0, "freed_bytes": 0}


def test_run_upload_cleanup_uses_db_ids(tmp_path):
    """Integration: live post id comes from the DB; dirs without a row are swept."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
    eng = create_async_engine(db_url)
    posts_root = tmp_path / "posts"

    async def _go():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SM = async_sessionmaker(eng, expire_on_commit=False)
        async with SM() as s:
            s.add(Post(id="keep-me", topic="t", format="single", status="draft"))
            await s.commit()
        _make_post_dir(posts_root, "keep-me")
        _make_post_dir(posts_root, "delete-me")
        res = await run_upload_cleanup(SM, posts_root)
        await eng.dispose()
        return res

    res = asyncio.run(_go())
    assert res["removed"] == 1
    assert (posts_root / "keep-me").exists()
    assert not (posts_root / "delete-me").exists()


def test_run_upload_cleanup_sweeps_library_files_whose_row_is_gone(tmp_path):
    """A library asset is reachable only through its row. Once the row goes the
    file is unreachable and just occupies disk — and video files are large
    enough that missing them is the difference between a tidy box and a full
    one. The reverse case must NOT be swept: a video that is still rendering
    has a row and no file yet, and deleting *rows* here would throw away work
    the user has already paid for."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    eng = create_async_engine(db_url)
    posts_root = tmp_path / "posts"
    media_root = tmp_path / "media"
    live = "11111111-1111-4111-8111-111111111111"
    gone = "22222222-2222-4222-8222-222222222222"
    pending = "33333333-3333-4333-8333-333333333333"

    async def _go():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SM = async_sessionmaker(eng, expire_on_commit=False)
        async with SM() as s:
            s.add(MediaAsset(id=live, user_id="u1", kind="image",
                             source="ai_gen", status="ready"))
            s.add(MediaAsset(id=pending, user_id="u1", kind="video",
                             source="ai_gen", status="pending"))
            await s.commit()
        media_store.save("u1", live, b"kept", "image/jpeg", root=media_root)
        media_store.save("u1", gone, b"orphan", "video/mp4", root=media_root)
        res = await run_upload_cleanup(SM, posts_root, media_root=media_root)
        await eng.dispose()
        return res

    res = asyncio.run(_go())
    assert res["media_removed"] == 1
    assert media_store.path_for("u1", live, root=media_root) is not None
    assert media_store.path_for("u1", gone, root=media_root) is None


def test_run_upload_cleanup_without_a_media_root_touches_nothing(tmp_path):
    """The job runs daily in prod against the real disk; a test that forgets to
    pass a root must not reach it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'n.db'}"
    eng = create_async_engine(db_url)

    async def _go():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SM = async_sessionmaker(eng, expire_on_commit=False)
        res = await run_upload_cleanup(SM, tmp_path / "posts",
                                       media_root=tmp_path / "absent")
        await eng.dispose()
        return res

    assert asyncio.run(_go())["media_removed"] == 0
