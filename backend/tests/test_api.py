"""Smoke tests for the endpoints that aren't covered elsewhere.

This file keeps liveness, the model catalogue, request validation, and
stock-search query validation. Post CRUD / generate / export / slide images live
in test_posts_crud_api.py; scheduling, insights and reels in
test_publishing_api.py; slide replacement in test_slide_replace.py.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db
from main import app
from models.database import Base


@pytest.fixture
def client(tmp_path):
    """TestClient with a throwaway sqlite DB (no lifespan side effects)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    eng = create_async_engine(db_url)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())

    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.state.sessionmaker = SM
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    asyncio.run(eng.dispose())


# ── liveness ────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_legal_pages_served(client):
    """ /terms and /privacy serve their own pages, not the SPA fallback. """
    t = client.get("/terms")
    assert t.status_code == 200 and "Terms of Service" in t.text
    p = client.get("/privacy")
    assert p.status_code == 200 and "Privacy Policy" in p.text


# ── model catalogue ─────────────────────────────────────────────────────────

def test_list_text_models(client):
    resp = client.get("/api/models/text")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) > 0
    assert all("id" in m and "name" in m and "provider" in m for m in data)


def test_list_image_models(client):
    resp = client.get("/api/models/image")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) > 0
    # every entry names the provider it belongs to, not an OpenRouter slug prefix
    assert all(m["provider"] in {"openrouter", "openai", "google"} for m in data)


def test_provider_catalogue(client):
    """Drives the Account dropdowns; must be usable before any key is entered."""
    resp = client.get("/api/models/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] and body["image"] and body["video"]
    assert {p["key"] for p in body["text"]} >= {"openrouter", "openai", "anthropic", "google"}
    # Anthropic has no image generation, so it must not be offered for images
    assert "anthropic" not in {p["key"] for p in body["image"]}
    # Video is a separate catalogue (Kling isn't a text/image provider)
    assert {p["key"] for p in body["video"]} == {"kling"}
    # the catalogue never carries secrets
    assert all("api_key" not in p for p in body["text"])


def test_list_video_models(client):
    resp = client.get("/api/models/video")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) > 0
    assert all(m["provider"] == "kling" for m in data)


# ── request validation ──────────────────────────────────────────────────────

def test_generate_post_rejects_short_topic(client):
    resp = client.post("/api/posts/generate", json={"topic": "AI", "format": "single"})
    assert resp.status_code == 422      # topic min_length=3


def test_generate_post_rejects_bad_format(client):
    resp = client.post("/api/posts/generate", json={"topic": "AI trends", "format": "nope"})
    assert resp.status_code == 422


def test_generate_post_rejects_malformed_niche_color(client):
    """Slide colours are per-tenant (any hex); only malformed values are rejected."""
    resp = client.post("/api/posts/generate", json={
        "topic": "AI trends", "format": "single", "niche_box_color": "royalblue",
    })
    assert resp.status_code == 422


# ── stock search validation ─────────────────────────────────────────────────

def test_stock_search_missing_query(client):
    resp = client.get("/api/stock/search")
    assert resp.status_code == 422


def test_stock_search_short_query(client):
    resp = client.get("/api/stock/search", params={"query": "a"})
    assert resp.status_code == 422      # min_length=2


def test_stock_search_bad_source(client):
    resp = client.get("/api/stock/search", params={"query": "running", "source": "flickr"})
    assert resp.status_code == 422      # pattern ^(unsplash|pexels)$


# ── 404s ────────────────────────────────────────────────────────────────────

def test_get_post_not_found(client):
    resp = client.get("/api/posts/does-not-exist")
    assert resp.status_code == 404


# ── C1: in cloud mode, protected routes require a valid JWT ─────────────────
# (Local mode has no auth gate — get_current_user returns the implicit local user.)

import pytest as _pytest  # noqa: E402
from api.deps import get_settings as _get_settings  # noqa: E402
from config import Settings as _Settings  # noqa: E402


@_pytest.fixture
def cloud_client(client):
    app.dependency_overrides[_get_settings] = lambda: _Settings(app_mode="cloud")
    yield client
    app.dependency_overrides.pop(_get_settings, None)


@_pytest.mark.parametrize("path", [
    "/api/stock/search?query=run",
    "/api/models/text",
    "/api/models/image",
    "/api/models/video",
    "/api/models/providers",
])
def test_protected_routes_401_without_jwt(cloud_client, path):
    assert cloud_client.get(path).status_code == 401


def test_protected_route_passes_with_jwt(cloud_client):
    reg = cloud_client.post("/api/auth/register",
                            json={"email": "c1@example.com", "password": "password123"})
    token = reg.json()["access_token"]
    res = cloud_client.get("/api/models/text", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code != 401


def test_local_mode_needs_no_token(client):
    # Regression guard: the desktop (local mode) must keep working without a login.
    assert client.get("/api/models/text").status_code == 200


# ── C2: unknown /api/* must 404, not fall through to the SPA ─────────────────

def test_unknown_api_path_returns_404(client):
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404
    assert "text/html" not in res.headers.get("content-type", "")


# ── C3: GET /api/usage must not write when nothing is buffered ──────────────

def test_usage_flush_noop_when_buffer_empty():
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, patch
    from api.routes.admin import _flush_usage

    db = AsyncMock()
    with patch("api.routes.admin.drain_usage", return_value=[]):
        _asyncio.get_event_loop().run_until_complete(_flush_usage(db)) if False else _asyncio.run(_flush_usage(db))
    db.commit.assert_not_awaited()


def test_usage_flush_commits_when_records_present():
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, patch
    from api.routes.admin import _flush_usage

    db = AsyncMock()
    rec = {"model": "m", "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.01}
    with patch("api.routes.admin.drain_usage", return_value=[rec]):
        _asyncio.run(_flush_usage(db))
    db.commit.assert_awaited_once()
