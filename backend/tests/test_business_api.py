"""Business API (Phase 2): sources + leads with workspace isolation.

The workspace_id filter is the mutation guard — user B must never see or touch
user A's sources/leads.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api.routes.business as business_routes
from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base
from services.sources.base import FetchedItem


class _FakeFetcher:
    def __init__(self, items):
        self._items = items

    async def fetch(self, url, since=None):
        return self._items


def _items():
    return [FetchedItem(external_id="1", kind="github_releases", title="New pricing tier",
                        url="https://ex.com/1", published_at=None, body="Prices changed.")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'biz.db'}")

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
    app.state.sessionmaker = SM
    monkeypatch.setattr(business_routes, "poll_source", _fake_poll_source)
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


async def _fake_poll_source(db, source, ssl_verify=True):
    """Stub the fetch/network: create one lead for the source, like a real poll."""
    from models.database import Lead
    db.add(Lead(workspace_id=source.workspace_id, source_id=source.id, external_id="1",
                what_happened="New pricing tier", source_url="https://ex.com/1",
                quote="Prices changed.", strength="worthy", reason="affects customers",
                status="new", raw={}))
    source.status = "ok"
    return 1


def _register(client, email, account_type="business"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "account_type": account_type})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_creator_account_gets_403(client):
    h = _register(client, "creator@ex.com", account_type="creator")
    assert client.get("/api/business/sources", headers=h).status_code == 403


def test_add_source_primes_leads(client):
    h = _register(client, "a@ex.com")
    r = client.post("/api/business/sources", headers=h,
                    json={"url": "https://github.com/o/r"})
    assert r.status_code == 200
    body = r.json()
    assert body["leads_found"] == 1
    assert body["source"]["kind"] == "github_releases"
    assert len(client.get("/api/business/sources", headers=h).json()) == 1
    leads = client.get("/api/business/leads", headers=h).json()
    assert len(leads) == 1 and leads[0]["strength"] == "worthy"


def test_workspace_isolation(client):
    ha = _register(client, "a2@ex.com")
    hb = _register(client, "b2@ex.com")
    src = client.post("/api/business/sources", headers=ha,
                      json={"url": "https://github.com/o/r"}).json()["source"]
    lead_id = client.get("/api/business/leads", headers=ha).json()[0]["id"]

    # B sees none of A's data, and can't fetch/delete A's rows.
    assert client.get("/api/business/leads", headers=hb).json() == []
    assert client.get("/api/business/sources", headers=hb).json() == []
    assert client.get(f"/api/business/leads/{lead_id}", headers=hb).status_code == 404
    assert client.delete(f"/api/business/sources/{src['id']}", headers=hb).status_code == 404


def test_dismiss_and_snooze_change_status(client):
    h = _register(client, "c@ex.com")
    client.post("/api/business/sources", headers=h, json={"url": "https://github.com/o/r"})
    lead_id = client.get("/api/business/leads", headers=h).json()[0]["id"]

    assert client.post(f"/api/business/leads/{lead_id}/dismiss", headers=h).status_code == 200
    assert client.get(f"/api/business/leads/{lead_id}", headers=h).json()["status"] == "dismissed"

    assert client.post(f"/api/business/leads/{lead_id}/snooze-kind", headers=h).status_code == 200
    assert client.get(f"/api/business/leads/{lead_id}", headers=h).json()["status"] == "snoozed_kind"


def test_refresh_triggers_poll(client):
    h = _register(client, "d@ex.com")
    src = client.post("/api/business/sources", headers=h,
                      json={"url": "https://github.com/o/r"}).json()["source"]
    r = client.post(f"/api/business/sources/{src['id']}/refresh", headers=h)
    assert r.status_code == 200 and r.json()["leads_found"] == 1


# ── over quota is not the same as broken ─────────────────────────────────────
#
# SourceRateLimited subclasses SourceFetchError so every existing handler keeps
# working, which is exactly why these two routes swallowed it: they catch the
# parent and write "unreachable". Being over GitHub's hourly quota then looks
# identical to a dead URL, and the screen invites the user to delete a source
# that works perfectly.

def _raises(exc):
    async def _poll(db, source, ssl_verify=True):
        raise exc
    return _poll


def _add(client, h):
    return client.post("/api/business/sources", headers=h,
                       json={"url": "https://github.com/o/r"}).json()["source"]


def test_a_rate_limited_source_is_not_marked_unreachable(client, monkeypatch):
    from services.sources.base import SourceRateLimited

    h = _register(client, "rl-add@ex.com")
    monkeypatch.setattr(business_routes, "poll_source",
                        _raises(SourceRateLimited("GitHub: 60/hour")))
    src = _add(client, h)
    assert src["status"] == "rate_limited"


def test_a_genuinely_dead_source_is_still_unreachable(client, monkeypatch):
    """The other half: catching the subclass first must not swallow the parent."""
    from services.sources.base import SourceFetchError

    h = _register(client, "dead-add@ex.com")
    monkeypatch.setattr(business_routes, "poll_source",
                        _raises(SourceFetchError("404")))
    assert _add(client, h)["status"] == "unreachable"


def test_refreshing_a_rate_limited_source_says_so(client, monkeypatch):
    from services.sources.base import SourceRateLimited

    h = _register(client, "rl-ref@ex.com")
    src = _add(client, h)
    monkeypatch.setattr(business_routes, "poll_source",
                        _raises(SourceRateLimited("GitHub: 60/hour")))
    r = client.post(f"/api/business/sources/{src['id']}/refresh", headers=h)

    # Distinguishable from a dead source, in the status AND in the answer —
    # 503, because our own 429 already means "you are clicking too fast", and
    # reusing it for "GitHub's quota" would recreate the confusion being fixed.
    assert r.status_code == 503
    assert "quota" in r.json()["detail"].lower()
    listed = client.get("/api/business/sources", headers=h).json()[0]
    assert listed["status"] == "rate_limited"


def test_refreshing_a_dead_source_still_reports_unreachable(client, monkeypatch):
    from services.sources.base import SourceFetchError

    h = _register(client, "dead-ref@ex.com")
    src = _add(client, h)
    monkeypatch.setattr(business_routes, "poll_source", _raises(SourceFetchError("404")))
    r = client.post(f"/api/business/sources/{src['id']}/refresh", headers=h)
    assert r.status_code == 502
    assert client.get("/api/business/sources", headers=h).json()[0]["status"] == "unreachable"


def test_the_status_enum_knows_every_status_the_poller_writes(client):
    """source_poller.py writes "rate_limited" and the SPA renders a badge for it,
    but the enum that documents the vocabulary never learned the word."""
    from models.schemas import SourceStatus
    assert {s.value for s in SourceStatus} >= {
        "ok", "unreachable", "rate_limited", "format_changed"}


def test_brand_rules_round_trip_and_isolation(client):
    ha = _register(client, "br-a@ex.com")
    hb = _register(client, "br-b@ex.com")
    # default empty
    assert client.get("/api/business/brand-rules", headers=ha).json() == {
        "forbidden": [], "required_disclaimers": []}
    # save + read back (blank lines stripped)
    client.put("/api/business/brand-rules", headers=ha,
               json={"forbidden": ["guaranteed", " "], "required_disclaimers": ["not advice"]})
    assert client.get("/api/business/brand-rules", headers=ha).json() == {
        "forbidden": ["guaranteed"], "required_disclaimers": ["not advice"]}
    # user B's rules are separate
    assert client.get("/api/business/brand-rules", headers=hb).json() == {
        "forbidden": [], "required_disclaimers": []}


def test_limits_round_trip_and_isolation(client):
    ha = _register(client, "lim-a@ex.com")
    hb = _register(client, "lim-b@ex.com")
    assert client.get("/api/business/limits", headers=ha).json() == {
        "max_per_day": None, "max_per_week": None}
    client.put("/api/business/limits", headers=ha,
               json={"max_per_day": 3, "max_per_week": 10})
    assert client.get("/api/business/limits", headers=ha).json() == {
        "max_per_day": 3, "max_per_week": 10}
    assert client.get("/api/business/limits", headers=hb).json() == {
        "max_per_day": None, "max_per_week": None}
    # out-of-range → 422
    assert client.put("/api/business/limits", headers=ha,
                      json={"max_per_day": 999}).status_code == 422


def test_adding_a_source_shows_what_it_actually_found(client):
    """A page that builds itself with JavaScript still answers, and the fetcher
    still extracts something from the shell — railway.com/changelog yields one
    entry reading "Weekly product updates since 2021". The source is added, looks
    healthy, and will never produce anything.

    No threshold separates that from a real changelog with one short entry: this
    file's own fixture is a single lead of thirty characters, indistinguishable
    from the broken case. One example of a broken page is not a distribution, and
    a guess dressed as a rule is worse than nothing.

    So the product does not guess — it shows what it pulled out. Nonsense is
    obvious to the person who chose the URL, immediately, while they are still
    looking at the field they typed it into.
    """
    h = _register(client, "sample@ex.com")

    r = client.post("/api/business/sources", headers=h,
                    json={"url": "https://github.com/o/r"})

    assert r.status_code == 200
    assert r.json()["sample"] == "New pricing tier"


def test_a_source_that_found_nothing_says_nothing_rather_than_guessing(client, monkeypatch):
    """No leads means no sample — and no invented explanation for why."""
    async def _empty(db, source, ssl_verify=True):
        source.status = "ok"
        return 0

    monkeypatch.setattr(business_routes, "poll_source", _empty)
    h = _register(client, "empty@ex.com")

    r = client.post("/api/business/sources", headers=h,
                    json={"url": "https://ex.com/changelog"})

    assert r.status_code == 200
    assert r.json()["sample"] is None
