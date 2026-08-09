"""What the product has already shown this person.

UX phase 8 makes features appear at the moment they start to mean something.
Every one of those moments needs the same thing underneath: a fact that survives
a reload, a new browser and a different device, and that can only ever be set
once.

Two rules carry the whole design.

**A milestone records what was SHOWN, never what was counted.** How many posts
somebody has made is a question the posts table answers; copying it here would
create a second number to disagree with the first. What lives here is only what
the data cannot reconstruct — that a hint was displayed, that somebody waved it
away, that an edit happened at a moment nobody kept.

**A milestone is never un-recorded.** A feature that appeared and then vanished
because a count dipped is worse than a feature that was always there: the first
one makes people doubt what they saw.
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import Base, User
from services import milestones


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ms.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _user(sm, email="ms@example.com") -> str:
    async def _go():
        async with sm() as db:
            user = User(email=email)
            db.add(user)
            await db.commit()
            return user.id
    return asyncio.run(_go())


def _run(sm, fn):
    async def _go():
        async with sm() as db:
            return await fn(db)
    return asyncio.run(_go())


def _record(sm, user_id: str, name: str):
    async def _go(db):
        user = await db.get(User, user_id)
        return await milestones.record(db, user, name)
    return _run(sm, _go)


def _all(sm, user_id: str) -> dict:
    async def _go(db):
        return milestones.all_for(await db.get(User, user_id))
    return _run(sm, _go)


# ── the basics ──────────────────────────────────────────────────────────────

def test_a_new_account_has_reached_nothing(sm):
    uid = _user(sm)

    assert _all(sm, uid) == {}
    async def _go(db):
        return milestones.reached(await db.get(User, uid), milestones.EDITED_AI_TEXT)
    assert _run(sm, _go) is False


def test_reaching_one_records_when(sm):
    uid = _user(sm)
    _record(sm, uid, milestones.EDITED_AI_TEXT)

    recorded = _all(sm, uid)
    assert list(recorded) == [milestones.EDITED_AI_TEXT]
    assert recorded[milestones.EDITED_AI_TEXT]          # a timestamp, not just True


def test_it_survives_the_session_that_set_it(sm):
    """Committed inside record(), like the free-post allowance: the caller's next
    move is usually to render a screen, and a milestone still pending on a
    session that a failure would roll back is a hint shown twice."""
    uid = _user(sm)
    _record(sm, uid, milestones.EDITED_AI_TEXT)

    async def _fresh(db):
        row = (await db.execute(select(User.milestones)
                                .where(User.id == uid))).scalar_one()
        return row or {}
    assert milestones.EDITED_AI_TEXT in _run(sm, _fresh)


def test_a_milestone_keeps_its_first_timestamp(sm):
    """Reached once, and the moment it was reached is the interesting one. An
    overwrite would quietly turn "when did this person first edit our text" into
    "when did they last"."""
    uid = _user(sm)
    _record(sm, uid, milestones.EDITED_AI_TEXT)
    first = _all(sm, uid)[milestones.EDITED_AI_TEXT]

    _record(sm, uid, milestones.EDITED_AI_TEXT)

    assert _all(sm, uid)[milestones.EDITED_AI_TEXT] == first


def test_recording_one_leaves_the_others_alone(sm):
    uid = _user(sm)
    _record(sm, uid, milestones.EDITED_AI_TEXT)
    _record(sm, uid, milestones.SOURCES_OFFERED)

    assert set(_all(sm, uid)) == {milestones.EDITED_AI_TEXT, milestones.SOURCES_OFFERED}


# ── the guards ──────────────────────────────────────────────────────────────

def test_an_unknown_name_is_refused(sm):
    """A typo in a milestone name is a feature that never appears and a test that
    never fails. The set is small and closed on purpose."""
    uid = _user(sm)

    with pytest.raises(ValueError):
        _record(sm, uid, "edited_ai_txet")


def test_one_account_cannot_see_another_s(sm):
    first = _user(sm, "first@example.com")
    second = _user(sm, "second@example.com")
    _record(sm, first, milestones.EDITED_AI_TEXT)

    assert _all(sm, second) == {}


def test_everything_can_be_revealed_at_once(sm):
    """The escape hatch. Somebody who saw a feature on a colleague's screen must
    be able to reach it in the product rather than in a support conversation."""
    uid = _user(sm)

    async def _go(db):
        return await milestones.record_all(db, await db.get(User, uid))
    _run(sm, _go)

    assert set(_all(sm, uid)) == set(milestones.REVEALABLE)


def test_revealing_features_does_not_invent_a_history(sm):
    """"Show all features" unlocks features. It must not also record that this
    person rewrote a caption or was told something once — those are claims about
    what happened, they leave in the GDPR export, and asserting them would
    silence hints nobody has seen yet."""
    uid = _user(sm)

    async def _go(db):
        return await milestones.record_all(db, await db.get(User, uid))
    _run(sm, _go)

    reached = _all(sm, uid)
    for name in (milestones.EDITED_AI_TEXT, milestones.RULES_HINT_DISMISSED,
                 milestones.SOURCES_OFFERED, milestones.CONNECTIONS_ARE_SHARED):
        assert name not in reached, name


def test_show_everything_does_not_rewrite_what_was_already_reached(sm):
    """It reveals; it does not rewrite history."""
    uid = _user(sm)
    _record(sm, uid, milestones.EDITED_AI_TEXT)
    first = _all(sm, uid)[milestones.EDITED_AI_TEXT]

    async def _go(db):
        return await milestones.record_all(db, await db.get(User, uid))
    _run(sm, _go)

    assert _all(sm, uid)[milestones.EDITED_AI_TEXT] == first


def test_a_row_that_predates_the_column_reads_as_empty(sm):
    """NULL is every account that existed before this migration. Treating it as
    "nothing shown yet" is the only reading that does not crash on them."""
    uid = _user(sm)

    async def _blank(db):
        user = await db.get(User, uid)
        user.milestones = None
        await db.commit()
    _run(sm, _blank)

    assert _all(sm, uid) == {}


# ── through the API ─────────────────────────────────────────────────────────
#
# The browser is the only witness to half of these — a hint displayed, a hint
# waved away — so there has to be a way to say so, and it has to refuse names
# nobody defined.


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from api.deps import get_db, get_settings
    from config import Settings
    from main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ms_api.db'}"
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
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url=db_url, api_token="", app_mode="cloud")
    app.state.sessionmaker = SM
    yield TestClient(app)
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)
    asyncio.run(eng.dispose())


def _headers(client, email="api@example.com") -> dict:
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_a_new_account_reports_none_over_the_api(client):
    headers = _headers(client)
    assert client.get("/api/settings/milestones", headers=headers).json() == {"milestones": {}}


def test_the_browser_can_record_what_only_it_saw(client):
    headers = _headers(client)
    r = client.post(f"/api/settings/milestones/{milestones.RULES_HINT_DISMISSED}",
                    headers=headers)

    assert r.status_code == 200
    assert milestones.RULES_HINT_DISMISSED in r.json()["milestones"]
    again = client.get("/api/settings/milestones", headers=headers).json()
    assert milestones.RULES_HINT_DISMISSED in again["milestones"]


def test_a_name_nobody_defined_is_refused(client):
    headers = _headers(client)
    r = client.post("/api/settings/milestones/whatever_i_like", headers=headers)

    assert r.status_code == 422
    assert client.get("/api/settings/milestones", headers=headers).json() == {"milestones": {}}


def test_show_all_features_reveals_everything(client):
    headers = _headers(client)
    r = client.post("/api/settings/milestones-all", headers=headers)

    assert set(r.json()["milestones"]) == set(milestones.REVEALABLE)


def test_milestones_need_an_account(client):
    assert client.get("/api/settings/milestones").status_code == 401
    assert client.post("/api/settings/milestones-all").status_code == 401


def test_one_account_never_reads_another_s_over_the_api(client):
    first = _headers(client, "one@example.com")
    second = _headers(client, "two@example.com")
    client.post("/api/settings/milestones-all", headers=first)

    assert client.get("/api/settings/milestones", headers=second).json() == {"milestones": {}}
