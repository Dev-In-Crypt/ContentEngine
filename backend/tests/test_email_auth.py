"""Email verification, password reset, rate limiting, and the verified-email
publish gate (PART XVIII Phase A)."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api.routes.auth as auth_routes
import services.auth as auth
from api.deps import get_db, get_settings, require_business, require_verified
from config import Settings
from main import app
from models.database import Base, User


# ── purpose tokens (pure) ────────────────────────────────────────────────────

def test_purpose_token_round_trip():
    tok = auth.create_purpose_token("u-1", "verify", timedelta(hours=1))
    assert auth.decode_purpose_token(tok, "verify") == "u-1"


def test_purpose_token_wrong_purpose_rejected():
    tok = auth.create_purpose_token("u-1", "verify", timedelta(hours=1))
    assert auth.decode_purpose_token(tok, "reset") is None


def test_purpose_token_cannot_authenticate():
    """A verify/reset token must never pass as an access token."""
    tok = auth.create_purpose_token("u-1", "verify", timedelta(hours=1))
    assert auth.decode_access_token(tok) is None


def test_purpose_token_expired_returns_none():
    from datetime import datetime, timezone
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    tok = auth.create_purpose_token("u-1", "reset", timedelta(hours=1), now=long_ago)
    assert auth.decode_purpose_token(tok, "reset") is None


# ── require_verified dependency (the publish gate) ───────────────────────────

def _user(**kw):
    return User(email="x@example.com", **kw)


def test_require_verified_blocks_unverified_when_enforced():
    s = Settings(app_mode="cloud", require_verified_email=True)
    with pytest.raises(HTTPException) as exc:
        require_verified(s, _user(email_verified=False, is_local=False))
    assert exc.value.status_code == 403


def test_require_verified_allows_verified_user():
    s = Settings(app_mode="cloud", require_verified_email=True)
    assert require_verified(s, _user(email_verified=True, is_local=False)) is None


def test_require_verified_noop_when_flag_off():
    s = Settings(app_mode="cloud", require_verified_email=False)
    assert require_verified(s, _user(email_verified=False, is_local=False)) is None


def test_require_verified_exempts_local_user():
    s = Settings(app_mode="cloud", require_verified_email=True)
    assert require_verified(s, _user(email_verified=False, is_local=True)) is None


# ── require_business gate (Business Phase 0) ─────────────────────────────────

def test_require_business_allows_business_account():
    assert require_business(_user(account_type="business", is_local=False)) is None


def test_require_business_blocks_creator_account():
    with pytest.raises(HTTPException) as exc:
        require_business(_user(account_type="creator", is_local=False))
    assert exc.value.status_code == 403


def test_require_business_blocks_unset_account_type():
    """A NULL account_type (legacy row) is treated as creator → blocked."""
    with pytest.raises(HTTPException) as exc:
        require_business(_user(account_type=None, is_local=False))
    assert exc.value.status_code == 403


def test_require_business_exempts_local_user():
    assert require_business(_user(account_type="creator", is_local=True)) is None


def test_require_business_blocks_agency_account():
    """An agency runs client brands through managed profiles, which is the
    creator engine — it is not the sources-to-approval Business product, and
    the gate must not widen just because a third value exists."""
    with pytest.raises(HTTPException) as exc:
        require_business(_user(account_type="agency", is_local=False))
    assert exc.value.status_code == 403


# ── endpoints (cloud mode) ───────────────────────────────────────────────────

@pytest.fixture
def cloud_client(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'email_auth.db'}"
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
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    app.state.sessionmaker = SM

    # Capture the tokens that would be emailed instead of hitting Resend.
    sent = {}
    monkeypatch.setattr(auth_routes, "send_verify_email",
                        AsyncMock(side_effect=lambda to, tok: sent.update(verify=tok)))
    monkeypatch.setattr(auth_routes, "send_reset_email",
                        AsyncMock(side_effect=lambda to, tok: sent.update(reset=tok)))

    client = TestClient(app)
    client.sent = sent
    yield client
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


def _verified(client, email):
    async def _q():
        async with app.state.sessionmaker() as s:
            u = (await s.execute(select(User).where(User.email == email))).scalar_one()
            return u.email_verified
    return asyncio.run(_q())


def test_register_sends_verify_and_verify_marks_user(cloud_client):
    r = cloud_client.post("/api/auth/register",
                          json={"email": "v@example.com", "password": "password123"})
    assert r.status_code == 200
    assert not _verified(cloud_client, "v@example.com")     # starts unverified

    token = cloud_client.sent["verify"]
    vr = cloud_client.get("/api/auth/verify", params={"token": token})
    assert vr.status_code == 200 and vr.json()["status"] == "verified"
    assert _verified(cloud_client, "v@example.com")         # now verified


def test_verify_rejects_bad_token(cloud_client):
    assert cloud_client.get("/api/auth/verify", params={"token": "garbage"}).status_code == 400


def test_forgot_then_reset_changes_password(cloud_client):
    cloud_client.post("/api/auth/register",
                      json={"email": "r@example.com", "password": "oldpassword"})
    # forgot always 200 (no email enumeration) and emits a reset token
    fr = cloud_client.post("/api/auth/forgot", json={"email": "r@example.com"})
    assert fr.status_code == 200
    token = cloud_client.sent["reset"]

    rr = cloud_client.post("/api/auth/reset",
                           json={"token": token, "password": "newpassword1"})
    assert rr.status_code == 200

    # old password no longer works, new one does
    assert cloud_client.post("/api/auth/login",
                             json={"email": "r@example.com", "password": "oldpassword"}
                             ).status_code == 401
    assert cloud_client.post("/api/auth/login",
                             json={"email": "r@example.com", "password": "newpassword1"}
                             ).status_code == 200


def test_forgot_unknown_email_still_200(cloud_client):
    """Must not reveal whether an address is registered."""
    r = cloud_client.post("/api/auth/forgot", json={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert "reset" not in cloud_client.sent      # no token emitted for unknown user


def test_register_rate_limited_returns_429(cloud_client):
    """The one test that exercises the limiter: re-enable it, then exceed 5/min."""
    from api.ratelimit import limiter
    limiter.enabled = True
    limiter.reset()
    try:
        codes = [
            cloud_client.post("/api/auth/register",
                              json={"email": f"rl{i}@example.com", "password": "password123"}
                              ).status_code
            for i in range(7)
        ]
    finally:
        limiter.enabled = False
    assert 429 in codes                     # limit tripped within the burst
    assert codes.count(200) <= 5            # at most the allowed 5 succeeded
