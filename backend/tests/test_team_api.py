"""Team invitations — the agency screen that deliberately opens no doors yet.

The whole feature is one table and four routes, and almost all of the risk is
in who may touch a row. Three separate things have to hold, and each has its
own test because each fails differently:

  * only an agency may invite at all (`require_agency`),
  * a pending invitation is unique per (owner, email) — enforced by a partial
    index, so a race produces an IntegrityError rather than two live rows,
  * an invitation may be accepted only by the address it was sent to.

The last one is the one that would actually hurt: a token is a bearer string,
and if the accept route trusts it without checking who is holding it, anybody
who sees a forwarded invitation email joins somebody else's team.

There is no role column on purpose. A role nothing enforces is a lie in the
schema, and access itself is a later phase — which the screen says out loud.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.email as email_service
from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base


@pytest.fixture
def client(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'team.db'}")

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
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


@pytest.fixture
def sent(monkeypatch):
    """Capture invitation emails instead of dispatching them."""
    box = []

    async def _fake(to, token, owner_email):
        box.append({"to": to, "token": token, "owner": owner_email})
        return True

    monkeypatch.setattr(email_service, "send_team_invite_email", _fake)
    import api.routes.team as team_routes
    monkeypatch.setattr(team_routes, "send_team_invite_email", _fake)
    return box


def _register(client, email, account_type="agency"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123",
                          "account_type": account_type})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── the gate ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("account_type", ["creator", "business"])
def test_only_an_agency_may_see_the_team_screen(client, account_type):
    h = _register(client, f"{account_type}@ex.com", account_type=account_type)
    assert client.get("/api/team/invitations", headers=h).status_code == 403
    assert client.post("/api/team/invitations", headers=h,
                       json={"email": "who@ex.com"}).status_code == 403


def test_an_agency_gets_an_empty_list_to_start(client):
    h = _register(client, "agency@ex.com")
    r = client.get("/api/team/invitations", headers=h)
    assert r.status_code == 200 and r.json() == []


# ── inviting ────────────────────────────────────────────────────────────────

def test_inviting_records_the_row_and_sends_the_email(client, sent):
    h = _register(client, "owner@ex.com")
    r = client.post("/api/team/invitations", headers=h, json={"email": "New@Ex.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Addresses are stored folded, or "a@x" and "A@X" are two pending invitations
    # to one person and the uniqueness index never fires.
    assert body["email"] == "new@ex.com"
    assert body["status"] == "pending"
    assert "token" not in body           # the token belongs in the email, not the list
    assert len(sent) == 1 and sent[0]["to"] == "new@ex.com"
    assert client.get("/api/team/invitations", headers=h).json()[0]["id"] == body["id"]


def test_a_second_pending_invitation_to_the_same_address_is_refused(client, sent):
    h = _register(client, "owner2@ex.com")
    assert client.post("/api/team/invitations", headers=h,
                       json={"email": "dup@ex.com"}).status_code == 200
    r = client.post("/api/team/invitations", headers=h, json={"email": "DUP@ex.com"})
    assert r.status_code == 409
    assert len(sent) == 1                # and no second email went out


def test_two_agencies_may_invite_the_same_person(client, sent):
    """The index is on (owner, email), not on email: one contractor can be
    invited by two agencies, and neither may see the other's row."""
    ha = _register(client, "a1@ex.com")
    hb = _register(client, "b1@ex.com")
    assert client.post("/api/team/invitations", headers=ha,
                       json={"email": "shared@ex.com"}).status_code == 200
    assert client.post("/api/team/invitations", headers=hb,
                       json={"email": "shared@ex.com"}).status_code == 200
    assert len(client.get("/api/team/invitations", headers=ha).json()) == 1
    assert len(client.get("/api/team/invitations", headers=hb).json()) == 1


def test_revoking_frees_the_address_to_be_invited_again(client, sent):
    """A revoked row is not pending, so the partial index lets a fresh one in —
    which is the reason the index is partial rather than plain."""
    h = _register(client, "owner3@ex.com")
    inv = client.post("/api/team/invitations", headers=h,
                      json={"email": "again@ex.com"}).json()
    assert client.delete(f"/api/team/invitations/{inv['id']}", headers=h).status_code == 200
    assert client.get("/api/team/invitations", headers=h).json()[0]["status"] == "revoked"
    assert client.post("/api/team/invitations", headers=h,
                       json={"email": "again@ex.com"}).status_code == 200


def test_one_agency_cannot_revoke_another_agencys_invitation(client, sent):
    ha = _register(client, "a2@ex.com")
    hb = _register(client, "b2@ex.com")
    inv = client.post("/api/team/invitations", headers=ha,
                      json={"email": "target@ex.com"}).json()
    assert client.delete(f"/api/team/invitations/{inv['id']}", headers=hb).status_code == 404
    assert client.get("/api/team/invitations", headers=ha).json()[0]["status"] == "pending"


# ── accepting ───────────────────────────────────────────────────────────────

def test_the_invited_address_can_accept(client, sent):
    h = _register(client, "owner4@ex.com")
    client.post("/api/team/invitations", headers=h, json={"email": "guest@ex.com"})
    hg = _register(client, "guest@ex.com", account_type="creator")

    r = client.post("/api/team/invitations/accept", headers=hg,
                    json={"token": sent[0]["token"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    row = client.get("/api/team/invitations", headers=h).json()[0]
    assert row["status"] == "accepted" and row["accepted_at"]


def test_somebody_else_cannot_accept_an_invitation_addressed_to_you(client, sent):
    """The token is a bearer string and invitation emails get forwarded. Without
    the address check, whoever reads the mail joins the team."""
    h = _register(client, "owner5@ex.com")
    client.post("/api/team/invitations", headers=h, json={"email": "invited@ex.com"})
    intruder = _register(client, "intruder@ex.com", account_type="creator")

    r = client.post("/api/team/invitations/accept", headers=intruder,
                    json={"token": sent[0]["token"]})
    assert r.status_code == 403
    assert client.get("/api/team/invitations", headers=h).json()[0]["status"] == "pending"


def test_a_revoked_invitation_cannot_be_accepted(client, sent):
    h = _register(client, "owner6@ex.com")
    inv = client.post("/api/team/invitations", headers=h,
                      json={"email": "late@ex.com"}).json()
    client.delete(f"/api/team/invitations/{inv['id']}", headers=h)
    hl = _register(client, "late@ex.com", account_type="creator")
    assert client.post("/api/team/invitations/accept", headers=hl,
                       json={"token": sent[0]["token"]}).status_code == 400


def test_a_forged_token_is_refused(client, sent):
    h = _register(client, "owner7@ex.com")
    client.post("/api/team/invitations", headers=h, json={"email": "real@ex.com"})
    hr = _register(client, "real@ex.com", account_type="creator")
    assert client.post("/api/team/invitations/accept", headers=hr,
                       json={"token": "not-a-jwt"}).status_code == 400


def test_a_token_for_another_purpose_is_refused(client, sent):
    """create_purpose_token stamps a purpose, and decode checks it. A password
    reset token must not double as a team invitation."""
    from datetime import timedelta

    from services.auth import create_purpose_token

    h = _register(client, "owner8@ex.com")
    inv = client.post("/api/team/invitations", headers=h,
                      json={"email": "cross@ex.com"}).json()
    hc = _register(client, "cross@ex.com", account_type="creator")
    wrong = create_purpose_token(inv["id"], "password_reset", timedelta(days=7))
    assert client.post("/api/team/invitations/accept", headers=hc,
                       json={"token": wrong}).status_code == 400


# ── the Team screen keeps itself (UX phase 8.5) ─────────────────────────────

def test_inviting_somebody_unlocks_the_team_screen(client, sent):
    """The tab is gated on having more than one profile, but an agency that has
    already invited people must not lose the screen — and the invitations they
    sent from it — by dropping back to a single brand."""
    from sqlalchemy import select

    from models.database import User
    from services import milestones

    h = _register(client, "owner@ex.com")
    client.post("/api/team/invitations", headers=h, json={"email": "new@ex.com"})

    async def _reached():
        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(
                User.email == "owner@ex.com"))).scalar_one()
            return milestones.all_for(user)

    assert milestones.TEAM_UNLOCKED in asyncio.run(_reached())


def test_a_refused_invitation_unlocks_nothing(client, sent):
    """A duplicate is a 409 and no new row. Unlocking on it would hand somebody
    a screen on the strength of an invitation that was not created."""
    from sqlalchemy import select

    from models.database import User
    from services import milestones

    h = _register(client, "fresh@ex.com")

    async def _forget():
        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(
                User.email == "fresh@ex.com"))).scalar_one()
            user.milestones = {}
            await db.commit()

    client.post("/api/team/invitations", headers=h, json={"email": "dup@ex.com"})
    asyncio.run(_forget())
    r = client.post("/api/team/invitations", headers=h, json={"email": "dup@ex.com"})

    assert r.status_code == 409

    async def _reached():
        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(
                User.email == "fresh@ex.com"))).scalar_one()
            return milestones.all_for(user)

    assert milestones.TEAM_UNLOCKED not in asyncio.run(_reached())
