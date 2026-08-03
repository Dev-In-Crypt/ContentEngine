"""Account export and erasure.

Two rules shape every test here. Export must never hand back a live credential —
we hold other people's Instagram tokens and paid X keys, and a downloadable file
is the easiest place to lose them. And erasure must be total but surgical: one
row of another tenant's data surviving is a leak, destroying one of theirs is
worse.
"""
import asyncio
import json
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import (
    AuditEntry, Base, Lead, LLMUsage, MediaAsset, Post, Slide, Source, User,
    UserCredentials, VideoPublishJob, Workspace,
)
from services import gdpr
from services.auth import hash_password
from services.secrets import encrypt


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gdpr.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


async def _seed(db, email):
    """One of everything the account owns, so a miss shows up as a count."""
    user = User(email=email, password_hash=hash_password("secret-password"),
                account_type="creator", niche="Bakery")
    db.add(user)
    await db.flush()
    db.add(UserCredentials(user_id=user.id,
                           instagram_access_token_enc=encrypt("ig-token-plaintext"),
                           openrouter_api_key_enc=encrypt("sk-or-secret")))
    post = Post(user_id=user.id, topic="Sourdough", caption="Fresh loaves",
                format="single", status="draft", platform="instagram")
    db.add(post)
    await db.flush()
    db.add(Slide(post_id=post.id, slide_number=1, image_source="ai",
                 image_path="/tmp/nope.jpg"))
    db.add(LLMUsage(user_id=user.id, model="m", cost=0.01))
    ws = Workspace(owner_user_id=user.id, name="Acme")
    db.add(ws)
    await db.flush()
    src = Source(workspace_id=ws.id, url="https://example.com/changelog",
                 kind="generic_page", status="ok")
    db.add(src)
    await db.flush()
    db.add(Lead(workspace_id=ws.id, source_id=src.id, what_happened="Shipped v2",
                strength="worthy", status="new"))
    db.add(AuditEntry(workspace_id=ws.id, post_id=post.id, ai_draft="first draft"))
    asset = MediaAsset(user_id=user.id, kind="video", source="ai_gen", status="ready",
                       provider="kling", prompt="a loaf cooling on a rack",
                       file_path="/tmp/nope.mp4", mime="video/mp4", duration_sec=5.0)
    db.add(asset)
    await db.flush()
    job = VideoPublishJob(user_id=user.id, platform="x", asset_id=asset.id,
                          video_path="/tmp/nope.mp4", total_bytes=1234,
                          status="queued", caption="Fresh loaves")
    db.add(job)
    await db.commit()
    return {"user": user, "post": post, "ws": ws, "asset": asset, "job": job}


@pytest.fixture
def two(sm):
    """The account under test plus a bystander who must survive untouched."""
    async def _go():
        async with sm() as db:
            mine = await _seed(db, "mine@example.com")
            theirs = await _seed(db, "theirs@example.com")
            return {"mine": mine, "theirs": theirs}
    return asyncio.run(_go())


def _collect(sm, user):
    async def _go():
        async with sm() as db:
            return await gdpr.collect_user_data(db, await db.get(User, user.id))
    return asyncio.run(_go())


def _erase(sm, user, **kw):
    async def _go():
        async with sm() as db:
            counts = await gdpr.delete_user_data(db, await db.get(User, user.id), **kw)
            await db.commit()
            return counts
    return asyncio.run(_go())


def _rows(sm, model, clause):
    async def _go():
        async with sm() as db:
            return (await db.execute(select(model).where(clause))).scalars().all()
    return asyncio.run(_go())


# ------------------------------------------------------------------ collect


def test_export_carries_the_account_and_its_posts(sm, two):
    data = _collect(sm, two["mine"]["user"])
    assert data["account"]["email"] == "mine@example.com"
    assert [p["topic"] for p in data["posts"]] == ["Sourdough"]
    assert data["posts"][0]["slides"][0]["slide_number"] == 1


def test_export_never_contains_another_tenants_row(sm, two):
    data = _collect(sm, two["mine"]["user"])
    blob = json.dumps(data)
    assert "theirs@example.com" not in blob
    assert two["theirs"]["post"].id not in blob


def test_export_masks_credentials_instead_of_decrypting_them(sm, two):
    """The whole point: a leaked export must not be a working set of keys."""
    data = _collect(sm, two["mine"]["user"])
    blob = json.dumps(data)
    assert "ig-token-plaintext" not in blob
    assert "sk-or-secret" not in blob
    assert "_enc" not in blob                      # not the ciphertext either
    creds = data["credentials"]
    assert creds["instagram_access_token"]["set"] is True
    assert creds["openrouter_api_key"]["set"] is True
    assert creds["imgbb_api_key"]["set"] is False


def test_export_picks_up_a_new_credential_column_with_no_code_change(sm, two):
    """_credential_summary() introspects UserCredentials.__table__.columns for
    any *_enc suffix rather than naming fields one by one — this pins that a
    freshly added key (Kling) is reported without anyone having to remember to
    wire it in by hand."""
    async def _set_kling():
        async with sm() as db:
            user = two["mine"]["user"]
            creds = await db.get(UserCredentials, user.id)
            creds.kling_api_key_enc = encrypt("kling-secret-xyz")
            await db.commit()
    asyncio.run(_set_kling())

    data = _collect(sm, two["mine"]["user"])
    assert data["credentials"]["kling_api_key"]["set"] is True
    assert "kling-secret-xyz" not in json.dumps(data)


def test_export_includes_the_business_workspace(sm, two):
    ws = _collect(sm, two["mine"]["user"])["workspace"]
    assert ws["name"] == "Acme"
    assert [s["url"] for s in ws["sources"]] == ["https://example.com/changelog"]
    assert [le["what_happened"] for le in ws["leads"]] == ["Shipped v2"]
    assert [a["ai_draft"] for a in ws["audit"]] == ["first draft"]


def test_export_of_a_bare_account_still_produces_a_document(sm):
    """Someone who signed up and did nothing must still be able to export."""
    async def _go():
        async with sm() as db:
            user = User(email="empty@example.com", password_hash=hash_password("x"))
            db.add(user)
            await db.commit()
            return await gdpr.collect_user_data(db, user)
    data = asyncio.run(_go())
    assert data["account"]["email"] == "empty@example.com"
    assert data["posts"] == []
    assert data["workspace"] is None


def test_export_carries_the_media_library(sm, two):
    """A generated clip the user paid their own provider for is theirs, and the
    prompt that produced it is the part they cannot reconstruct from the file."""
    data = _collect(sm, two["mine"]["user"])
    assert [a["prompt"] for a in data["media_assets"]] == ["a loaf cooling on a rack"]
    assert data["media_assets"][0]["kind"] == "video"


def test_export_never_contains_another_tenants_asset(sm, two):
    data = _collect(sm, two["mine"]["user"])
    mine = {a["id"] for a in data["media_assets"]}
    assert two["theirs"]["asset"].id not in mine


def test_erase_removes_the_media_library(sm, two):
    _erase(sm, two["mine"]["user"])
    assert _rows(sm, MediaAsset, MediaAsset.user_id == two["mine"]["user"].id) == []
    # ...and leaves the bystander's library standing.
    assert len(_rows(sm, MediaAsset,
                     MediaAsset.user_id == two["theirs"]["user"].id)) == 1


def test_export_carries_video_publish_jobs(sm, two):
    """The tweet id and permalink are the part of a publish attempt the user
    cannot reconstruct from anywhere else once it's gone."""
    data = _collect(sm, two["mine"]["user"])
    assert [j["caption"] for j in data["video_publish_jobs"]] == ["Fresh loaves"]
    assert data["video_publish_jobs"][0]["status"] == "queued"


def test_export_never_contains_another_tenants_publish_job(sm, two):
    data = _collect(sm, two["mine"]["user"])
    mine = {j["id"] for j in data["video_publish_jobs"]}
    assert two["theirs"]["job"].id not in mine


def test_erase_removes_video_publish_jobs(sm, two):
    """Mutation guard: skip this _delete call and the row (which carries FKs to
    both the deleted post and the deleted asset) survives erasure as an orphan."""
    _erase(sm, two["mine"]["user"])
    assert _rows(sm, VideoPublishJob,
                VideoPublishJob.user_id == two["mine"]["user"].id) == []
    # ...and leaves the bystander's job standing.
    assert len(_rows(sm, VideoPublishJob,
                     VideoPublishJob.user_id == two["theirs"]["user"].id)) == 1


def test_media_paths_include_library_files(sm, two):
    """The export walks file_path to decide what goes in the ZIP; miss it and
    the archive quietly ships a manifest with no media behind it."""
    async def _go():
        async with sm() as db:
            user = await db.get(User, two["mine"]["user"].id)
            return await gdpr.user_media_paths(db, user)
    assert "/tmp/nope.mp4" in asyncio.run(_go())


def test_export_does_not_leak_the_password_hash(sm, two):
    data = _collect(sm, two["mine"]["user"])
    assert "password_hash" not in data["account"]
    assert "$argon2" not in json.dumps(data)


# ------------------------------------------------------------------ media


def test_media_only_leaves_files_that_live_under_uploads(tmp_path):
    """image_path is an absolute path out of the database. If one ever pointed at
    /etc/passwd, the export would happily hand it to the user."""
    uploads = tmp_path / "uploads"
    (uploads / "posts" / "p1").mkdir(parents=True)
    inside = uploads / "posts" / "p1" / "slide_1.jpg"
    inside.write_bytes(b"jpeg")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"password file")

    picked = gdpr.safe_media_files([str(inside), str(outside), None, ""], uploads)
    assert [p.name for p in picked] == ["slide_1.jpg"]


def test_media_arcnames_are_relative_to_uploads(tmp_path):
    uploads = tmp_path / "uploads"
    (uploads / "posts" / "p1").mkdir(parents=True)
    f = uploads / "posts" / "p1" / "slide_1.jpg"
    f.write_bytes(b"jpeg")
    assert gdpr.arcname_for(f, uploads) == "media/posts/p1/slide_1.jpg"


def test_a_symlink_out_of_uploads_is_not_followed(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"password file")
    link = uploads / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable to this user")
    assert gdpr.safe_media_files([str(link)], uploads) == []


# ------------------------------------------------------------------ zip


def test_the_zip_holds_the_document_and_the_media(tmp_path):
    uploads = tmp_path / "uploads"
    (uploads / "posts" / "p1").mkdir(parents=True)
    f = uploads / "posts" / "p1" / "slide_1.jpg"
    f.write_bytes(b"jpeg-bytes")
    out = tmp_path / "export.zip"

    gdpr.write_export_zip(out, {"account": {"email": "a@b.c"}}, [f], uploads)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "data.json" in names
        assert "media/posts/p1/slide_1.jpg" in names
        assert json.loads(zf.read("data.json"))["account"]["email"] == "a@b.c"
        assert zf.read("media/posts/p1/slide_1.jpg") == b"jpeg-bytes"


def test_a_file_that_vanished_mid_export_does_not_kill_the_zip(tmp_path):
    """Housekeeping can delete an orphan while we're building the archive."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    out = tmp_path / "export.zip"
    gdpr.write_export_zip(out, {"account": {}}, [uploads / "gone.jpg"], uploads)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["data.json"]


# ------------------------------------------------------------------ erase


def test_erase_removes_every_row_the_account_owned(sm, two):
    mine = two["mine"]["user"]
    _erase(sm, mine)
    assert _rows(sm, User, User.id == mine.id) == []
    assert _rows(sm, UserCredentials, UserCredentials.user_id == mine.id) == []
    assert _rows(sm, Post, Post.user_id == mine.id) == []
    assert _rows(sm, LLMUsage, LLMUsage.user_id == mine.id) == []
    assert _rows(sm, Workspace, Workspace.owner_user_id == mine.id) == []


def test_erase_reaches_the_grandchildren(sm, two):
    """Slides hang off posts and leads off the workspace — neither carries a
    user_id, so a delete that only walks the direct children orphans them."""
    post_id, ws_id = two["mine"]["post"].id, two["mine"]["ws"].id
    _erase(sm, two["mine"]["user"])
    assert _rows(sm, Slide, Slide.post_id == post_id) == []
    assert _rows(sm, Source, Source.workspace_id == ws_id) == []
    assert _rows(sm, Lead, Lead.workspace_id == ws_id) == []
    assert _rows(sm, AuditEntry, AuditEntry.workspace_id == ws_id) == []


def test_erase_leaves_the_other_tenant_completely_alone(sm, two):
    _erase(sm, two["mine"]["user"])
    theirs = two["theirs"]
    assert len(_rows(sm, User, User.id == theirs["user"].id)) == 1
    assert len(_rows(sm, Post, Post.user_id == theirs["user"].id)) == 1
    assert len(_rows(sm, Slide, Slide.post_id == theirs["post"].id)) == 1
    assert len(_rows(sm, Lead, Lead.workspace_id == theirs["ws"].id)) == 1
    assert len(_rows(sm, AuditEntry, AuditEntry.workspace_id == theirs["ws"].id)) == 1
    assert len(_rows(sm, UserCredentials,
                     UserCredentials.user_id == theirs["user"].id)) == 1


def test_erase_reports_what_it_destroyed(sm, two):
    counts = _erase(sm, two["mine"]["user"])
    assert counts["posts"] == 1
    assert counts["slides"] == 1
    assert counts["leads"] == 1
    assert counts["sources"] == 1


def test_erase_cancels_the_scheduled_publish_jobs(sm, two, monkeypatch):
    """A job left armed would fire on a post that no longer exists."""
    cancelled = []
    monkeypatch.setattr(gdpr, "cancel_publish", lambda pid: cancelled.append(pid))
    _erase(sm, two["mine"]["user"])
    assert cancelled == [two["mine"]["post"].id]


def test_erase_removes_the_accounts_directories(tmp_path):
    uploads = tmp_path / "uploads"
    mine = uploads / "posts" / "p1"
    theirs = uploads / "posts" / "p2"
    for d in (mine, theirs):
        d.mkdir(parents=True)
        (d / "slide_1.jpg").write_bytes(b"x")
    for name in ("logos", "music", "staging", "media"):
        (uploads / name / "u1").mkdir(parents=True)
        (uploads / name / "u1" / "f.bin").write_bytes(b"x")
        (uploads / name / "u2").mkdir(parents=True)

    removed = gdpr.delete_user_files("u1", ["p1"], uploads)

    assert not mine.exists()
    assert theirs.exists()                       # another tenant's post survives
    for name in ("logos", "music", "staging", "media"):
        assert not (uploads / name / "u1").exists()
        assert (uploads / name / "u2").exists()
    assert removed == 5


def test_erase_of_files_tolerates_an_account_that_uploaded_nothing(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    assert gdpr.delete_user_files("nobody", [], uploads) == 0


def test_a_post_id_cannot_escape_the_uploads_root(tmp_path):
    """post ids come from the database; a traversal one must not delete a parent."""
    uploads = tmp_path / "uploads"
    (uploads / "posts").mkdir(parents=True)
    victim = tmp_path / "keepme"
    victim.mkdir()
    (victim / "f.txt").write_bytes(b"x")

    gdpr.delete_user_files("u1", ["../../keepme"], uploads)
    assert victim.exists()
