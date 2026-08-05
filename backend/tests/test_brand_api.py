"""POST /api/brand/extract — the route behind "paste a link" (phase 1.7).

The point of this endpoint is that one request replaces a form with eight
fields, so most of what is tested here is what happens when a website is less
cooperative than the happy path: a private address, a dead host, a page with
nothing in it. None of those may cost the caller the parts that did work.
"""
import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pytest_httpx import HTTPXMock
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings, get_text_provider
from config import Settings
from main import app
from models.database import Base
from services import url_guard
from services.url_guard import BLOCKED_MESSAGE

URL = "https://acme.example/"


@pytest.fixture
def client(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'brand.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    # No text provider by default: the niche guess is the optional half, and
    # every test below that doesn't name it must pass without one.
    app.dependency_overrides[get_text_provider] = lambda: None
    app.state.sessionmaker = SM
    yield TestClient(app)
    for dep in (get_db, get_settings, get_text_provider):
        app.dependency_overrides.pop(dep, None)
    asyncio.run(eng.dispose())


def _register(client, email="brand@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123",
                          "account_type": "creator"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _page(httpx_mock: HTTPXMock, head: str, **kw):
    httpx_mock.add_response(
        text=f"<html><head>{head}</head><body></body></html>",
        headers={"content-type": "text/html"}, **kw)
    # /favicon.ico is always the last candidate, declared or not — most sites
    # serve one without saying so. Answering 404 here is the ordinary case of a
    # site that doesn't.
    httpx_mock.add_response(url="https://acme.example/favicon.ico",
                            status_code=404, is_optional=True)


def _set_text_model(client, headers, provider="openrouter",
                    model="anthropic/claude-sonnet-5"):
    r = client.put("/api/settings/ai", headers=headers,
                   json={"text_provider": provider, "text_model": model})
    assert r.status_code == 200


def _logo_bytes() -> bytes:
    img = Image.new("RGBA", (32, 32), (255, 255, 255, 255))
    img.paste((10, 37, 64, 255), (0, 0, 8, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class StubProvider:
    def __init__(self, reply):
        self.reply = reply

    async def generate_text(self, **kwargs):
        return (self.reply, [])


# ------------------------------------------------------------------ happy path

def test_a_site_comes_back_as_a_filled_in_profile(client, httpx_mock: HTTPXMock):
    _page(httpx_mock, """
      <title>Acme | Industrial Anvils</title>
      <meta property="og:description" content="We make anvils.">
      <meta name="theme-color" content="#0A2540">
      <link rel="apple-touch-icon" href="/touch.png">
    """)
    httpx_mock.add_response(url="https://acme.example/touch.png",
                            content=_logo_bytes(),
                            headers={"content-type": "image/png"})
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": URL})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Acme"
    assert body["description"] == "We make anvils."
    assert body["colors"][0] == "#0a2540"
    assert body["logo_data_url"].startswith("data:image/png;base64,")
    assert body["source_url"] == URL


def test_the_niche_is_guessed_when_a_model_is_configured(client,
                                                         httpx_mock: HTTPXMock):
    app.dependency_overrides[get_text_provider] = lambda: StubProvider(
        '{"niche": "industrial tooling", "target_audience": "factory buyers"}')
    _page(httpx_mock, '<title>Acme</title>'
                      '<meta name="description" content="We make anvils.">')
    headers = _register(client)
    _set_text_model(client, headers)
    r = client.post("/api/brand/extract", headers=headers, json={"url": URL})
    assert r.status_code == 200
    assert r.json()["niche"] == "industrial tooling"
    assert r.json()["target_audience"] == "factory buyers"


def test_no_text_model_still_returns_the_brand(client, httpx_mock: HTTPXMock):
    """Mutation guard: the niche is the one part of this that needs an AI key.
    Requiring one would put a paywall in front of the field that exists to make
    signing up easier. The provider is present but no model is chosen, which is
    exactly the state a brand-new account is in — and it must not be called."""
    called = []

    class _Recording(StubProvider):
        async def generate_text(self, **kwargs):
            called.append(kwargs)
            return await super().generate_text(**kwargs)

    app.dependency_overrides[get_text_provider] = lambda: _Recording('{"niche": "x"}')
    _page(httpx_mock, '<title>Acme</title>')
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": URL})
    assert r.status_code == 200
    assert r.json()["name"] == "Acme" and r.json()["niche"] == ""
    assert called == []


def test_a_site_with_nothing_to_say_is_not_an_error(client, httpx_mock: HTTPXMock):
    _page(httpx_mock, "")
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": URL})
    assert r.status_code == 200
    assert r.json()["name"] is None and r.json()["logo_data_url"] is None


# ------------------------------------------------------------------ refusals

def test_a_private_address_is_refused_with_the_uniform_message(client, monkeypatch,
                                                               httpx_mock: HTTPXMock):
    """The whole reason phase 1 exists. The working response is registered so
    an unguarded route would answer 200 and fail on the status assertion."""
    monkeypatch.setattr(url_guard, "_resolve", lambda host: ["169.254.169.254"])
    _page(httpx_mock, "<title>Internal</title>", is_optional=True)
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 400
    assert r.json()["detail"] == BLOCKED_MESSAGE


def test_localhost_is_refused_with_the_same_message(client, monkeypatch,
                                                    httpx_mock: HTTPXMock):
    """Mutation guard: a different message for a different failure is an oracle
    — repeat it against a port range and you have mapped our internal network.
    fact_check.py already puts guard text straight into an API response, so
    this is not hypothetical."""
    monkeypatch.setattr(url_guard, "_resolve", lambda host: ["127.0.0.1"])
    _page(httpx_mock, "<title>Ours</title>", is_optional=True)
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": "http://127.0.0.1:8000/health"})
    assert r.status_code == 400
    assert r.json()["detail"] == BLOCKED_MESSAGE


def test_a_dead_site_is_a_400_not_a_500(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=503, text="down")
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": URL})
    assert r.status_code == 400


def test_a_non_http_scheme_never_reaches_the_fetcher(client):
    r = client.post("/api/brand/extract", headers=_register(client),
                    json={"url": "file:///etc/passwd"})
    assert r.status_code == 422


def test_the_route_needs_a_token(client):
    """The landing page (phase 7) will want this without a login, and that is a
    separate decision with a rate limit and a spend ceiling attached (phase 6).
    Until then it is authed, and this is what says so."""
    r = client.post("/api/brand/extract", json={"url": URL})
    assert r.status_code in (401, 403)
