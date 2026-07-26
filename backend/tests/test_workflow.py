"""Business approval workflow (Phase 5): status transitions + publish gate.

The publish gate (a workspace post must be 'approved') is the mutation target:
allowing publish from draft would let a post skip the human.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api.routes.business as business_routes
import services.publisher_flow as publisher_flow
from api.deps import get_content_engine, get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Lead
from models.schemas import PostFormat, Platform
from services.content_engine import GeneratedPost


class _FakeEngine:
    def __init__(self, caption="Draft caption"):
        self._caption = caption

    async def generate_post(self, **kw):
        prog = kw.get("progress")
        if prog:
            await prog("generating")
        return GeneratedPost(
            id=str(uuid.uuid4()), topic=kw["topic"], format=PostFormat.SINGLE,
            caption=self._caption, hashtags=["#x"], cta="Go", hook="Hook",
            alt_text="", slides=[], text_model_used="m", image_model_used=None,
            platform=Platform.INSTAGRAM)


async def _fake_poll_source(db, source, ssl_verify=True):
    db.add(Lead(workspace_id=source.workspace_id, source_id=source.id,
                external_id=str(uuid.uuid4()), what_happened="New pricing tier",
                source_url="https://ex.com/1", quote="Prices changed.",
                strength="worthy", reason="affects customers", status="new", raw={}))
    source.status = "ok"
    return 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wf.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud",
                                                              require_verified_email=False)
    app.dependency_overrides[get_content_engine] = lambda: _FakeEngine()
    app.state.sessionmaker = SM
    monkeypatch.setattr(business_routes, "poll_source", _fake_poll_source)
    monkeypatch.setattr(business_routes, "resolve_ai_choice",
                        lambda user, settings, kind: ("openrouter", "m", "k"))
    yield TestClient(app), SM, monkeypatch
    for dep in (get_db, get_settings, get_content_engine):
        app.dependency_overrides.pop(dep, None)
    asyncio.run(eng.dispose())


def _register(c, email):
    r = c.post("/api/auth/register",
               json={"email": email, "password": "password123", "account_type": "business"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_draft(c, h):
    c.post("/api/business/sources", headers=h, json={"url": "https://github.com/o/r"})
    lead_id = c.get("/api/business/leads", headers=h).json()[0]["id"]
    c.post(f"/api/business/leads/{lead_id}/draft", headers=h)
    return c.get("/api/business/drafts", headers=h).json()[0]["id"]


def test_submit_then_approve(client):
    c, _, _ = client
    h = _register(c, "a@ex.com")
    pid = _make_draft(c, h)
    assert c.get("/api/business/drafts", headers=h).json()[0]["status"] == "draft"

    assert c.post(f"/api/business/posts/{pid}/submit", headers=h).json()["status"] == "in_review"
    assert c.post(f"/api/business/posts/{pid}/approve", headers=h).json()["status"] == "approved"


def test_approve_requires_in_review(client):
    c, _, _ = client
    h = _register(c, "b@ex.com")
    pid = _make_draft(c, h)
    # can't approve a draft that wasn't submitted
    assert c.post(f"/api/business/posts/{pid}/approve", headers=h).status_code == 409


def test_brand_forbidden_blocks_approval(client):
    c, _, _ = client
    h = _register(c, "c@ex.com")
    # forbid the exact caption the fake engine emits, THEN draft so the check catches it
    c.put("/api/business/brand-rules", headers=h,
          json={"forbidden": ["draft caption"], "required_disclaimers": []})
    pid = _make_draft(c, h)
    c.post(f"/api/business/posts/{pid}/submit", headers=h)
    r = c.post(f"/api/business/posts/{pid}/approve", headers=h)
    assert r.status_code == 409                      # brand-rule violation blocks approve


def test_publish_gate_blocks_unapproved(client):
    c, _, monkeypatch = client
    h = _register(c, "d@ex.com")
    pid = _make_draft(c, h)   # status draft
    # Business post can't publish until approved — mutation guard.
    assert c.post(f"/api/posts/{pid}/publish", headers=h).status_code == 409

    # Approve, then the gate lets it through (publish itself is stubbed).
    async def _fake_publish(sessionmaker, post_id):
        return "mid-123"
    monkeypatch.setattr(publisher_flow, "publish_now", _fake_publish)
    c.post(f"/api/business/posts/{pid}/submit", headers=h)
    c.post(f"/api/business/posts/{pid}/approve", headers=h)
    assert c.post(f"/api/posts/{pid}/publish", headers=h).status_code == 200


def test_workflow_isolation(client):
    c, _, _ = client
    ha = _register(c, "e@ex.com")
    hb = _register(c, "f@ex.com")
    pid = _make_draft(c, ha)
    assert c.post(f"/api/business/posts/{pid}/submit", headers=hb).status_code == 404


def test_a_failed_business_post_can_be_published_again(client):
    """A workspace post that was approved and then failed to publish used to be a
    dead end: publish demands "approved", and neither submit nor approve accepts
    "failed". The human sign-off already happened, so retrying must be allowed.

    Mutation guard: restore `!= "approved"` and the retry 409s forever.
    """
    import asyncio
    from models.database import Post as PostModel

    c, SM, monkeypatch = client
    h = _register(c, "retry@ex.com")
    pid = _make_draft(c, h)
    c.post(f"/api/business/posts/{pid}/submit", headers=h)
    c.post(f"/api/business/posts/{pid}/approve", headers=h)

    async def _fail(sessionmaker, post_id):
        async with SM() as db:
            post = await db.get(PostModel, post_id)
            post.status, post.schedule_error = "failed", "X rejected the media"
            await db.commit()
        raise publisher_flow.PublishError("X rejected the media")
    monkeypatch.setattr(publisher_flow, "publish_now", _fail)
    assert c.post(f"/api/posts/{pid}/publish", headers=h).status_code == 502

    async def _ok(sessionmaker, post_id):
        return "mid-456"
    monkeypatch.setattr(publisher_flow, "publish_now", _ok)
    assert c.post(f"/api/posts/{pid}/publish", headers=h).status_code == 200


def test_a_draft_business_post_still_cannot_publish(client):
    """The retry path must not become a way around human sign-off."""
    c, _, _ = client
    h = _register(c, "nodraft@ex.com")
    pid = _make_draft(c, h)
    assert c.post(f"/api/posts/{pid}/publish", headers=h).status_code == 409
