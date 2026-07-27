"""POST /api/posts/{id}/verify — the creator-side fact check over HTTP."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.fact_check as fact_check
from api.deps import get_content_engine, get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post

SOURCE = "Pricing update — we cut prices by 20% in June for every plan."


class _FakeProvider:
    def __init__(self):
        self.calls = []

    async def generate_text(self, **kw):
        self.calls.append(kw)
        return ('[{"claim":"Prices were cut by 20%","status":"confirmed",'
                f'"evidence":"{SOURCE}"}}]'), []


class _FakeEngine:
    def __init__(self):
        self.text_provider = _FakeProvider()
        self.caption_gen = self


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'verify.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    fake = _FakeEngine()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    app.dependency_overrides[get_content_engine] = lambda: fake
    app.state.sessionmaker = SM
    yield TestClient(app), SM, fake
    for dep in (get_db, get_settings, get_content_engine):
        app.dependency_overrides.pop(dep, None)
    asyncio.run(eng.dispose())


def _register(c, email="a@example.com"):
    r = c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed(SM, user_id, caption="We cut prices by 20%.", sources=None):
    pid = str(uuid.uuid4())

    async def _s():
        async with SM() as db:
            db.add(Post(id=pid, user_id=user_id, topic="Pricing", format="single",
                        status="preview", caption=caption, sources=sources or []))
            await db.commit()
    asyncio.run(_s())
    return pid


def _stored(SM, pid):
    async def _s():
        async with SM() as db:
            return (await db.execute(
                select(Post).where(Post.id == pid))).scalars().first().claim_check
    return asyncio.run(_s())


def test_verify_needs_a_login(ctx):
    c, SM, _ = ctx
    pid = _seed(SM, "nobody")
    assert c.post(f"/api/posts/{pid}/verify", json={}).status_code == 401


def test_another_users_post_is_not_verifiable(ctx):
    c, SM, _ = ctx
    ha = _register(c, "a@example.com")
    hb = _register(c, "b@example.com")
    bid = c.get("/api/auth/me", headers=hb).json()["id"]
    pid = _seed(SM, bid)
    assert c.post(f"/api/posts/{pid}/verify", json={}, headers=ha).status_code == 404


def test_a_post_with_nothing_to_check_against_says_so(ctx):
    """No citations, nothing pasted: an honest empty result, and no model call."""
    c, SM, fake = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid)

    r = c.post(f"/api/posts/{pid}/verify", json={}, headers=h)
    assert r.status_code == 200
    assert r.json()["checked_claims"] == []
    # The author must be able to tell this from "checked, nothing to flag".
    assert r.json()["fact_check"]["status"] == "no_source"
    assert _stored(SM, pid)["check"]["status"] == "no_source"
    assert fake.text_provider.calls == []


def test_pasted_source_produces_a_verdict_on_the_post(ctx):
    c, SM, fake = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid)

    r = c.post(f"/api/posts/{pid}/verify", json={"source_text": SOURCE}, headers=h)
    assert r.status_code == 200
    claims = r.json()["checked_claims"]
    assert claims and claims[0]["status"] == "confirmed"
    assert len(fake.text_provider.calls) == 1
    # the pasted source really reached the prompt
    assert SOURCE in fake.text_provider.calls[0]["user_prompt"]


def test_the_verdict_survives_a_reload(ctx):
    c, SM, _ = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid)
    c.post(f"/api/posts/{pid}/verify", json={"source_text": SOURCE}, headers=h)

    again = c.get(f"/api/posts/{pid}", headers=h)
    assert again.json()["checked_claims"][0]["status"] == "confirmed"
    assert again.json()["fact_check"]["status"] == "checked"


def test_cited_urls_are_fetched_and_used(ctx, monkeypatch):
    c, SM, fake = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid, sources=[{"title": "Pricing", "url": "https://ex.com/pricing"}])

    seen = []

    class _F:
        async def fetch(self, url, since=None):
            seen.append(url)
            from services.sources.base import FetchedItem
            return [FetchedItem(external_id="1", kind="generic_page", title="Pricing",
                                url=url, published_at=None, body=SOURCE)]

    monkeypatch.setattr(fact_check, "get_source_fetcher", lambda k, **kw: _F())
    monkeypatch.setattr(fact_check, "detect_source_type", lambda u: "generic_page")

    r = c.post(f"/api/posts/{pid}/verify", json={}, headers=h)
    assert seen == ["https://ex.com/pricing"]
    assert r.json()["checked_claims"][0]["status"] == "confirmed"
    assert _stored(SM, pid)["check"]["sources_used"][0]["ok"] is True


def test_a_business_drafts_brand_flags_are_not_wiped(ctx):
    """This endpoint owns the claim side only — re-checking must not erase the
    brand-rule verdict a Business draft already carries."""
    c, SM, _ = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid)

    async def _s():
        async with SM() as db:
            p = (await db.execute(select(Post).where(Post.id == pid))).scalars().first()
            p.claim_check = {"claims": [], "brand": {"forbidden_found": ["guaranteed"]}}
            await db.commit()
    asyncio.run(_s())

    c.post(f"/api/posts/{pid}/verify", json={"source_text": SOURCE}, headers=h)
    assert _stored(SM, pid)["brand"] == {"forbidden_found": ["guaranteed"]}


def test_an_over_long_paste_is_rejected_not_truncated_silently(ctx):
    c, SM, _ = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid)
    r = c.post(f"/api/posts/{pid}/verify", json={"source_text": "x" * 20001}, headers=h)
    assert r.status_code == 422


def test_no_ai_key_is_a_clear_message_not_a_stack_trace(ctx):
    """Reachable in one click before setup is finished. The reason must be about
    the missing key, not an AttributeError that reads like the post's fault."""
    c, SM, fake = ctx
    h = _register(c)
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    pid = _seed(SM, uid)
    fake.text_provider = None

    r = c.post(f"/api/posts/{pid}/verify", json={"source_text": SOURCE}, headers=h)
    assert r.status_code == 400
    assert "AI key" in r.json()["detail"]
    assert _stored(SM, pid) in (None, {})       # nothing recorded on the post
