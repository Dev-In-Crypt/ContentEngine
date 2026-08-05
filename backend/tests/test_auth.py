"""Auth: password hashing, JWTs, and the register/login/me endpoints."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.auth as auth
from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base


# ── pure functions ──────────────────────────────────────────────────────────

def test_hash_verify_round_trip():
    h = auth.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"      # not plaintext
    assert auth.verify_password("correct horse battery staple", h)


def test_verify_rejects_wrong_password():
    h = auth.hash_password("right")
    assert not auth.verify_password("wrong", h)


def test_verify_rejects_empty_hash():
    assert not auth.verify_password("anything", "")


def test_jwt_round_trip():
    token = auth.create_access_token("user-123")
    assert auth.decode_access_token(token) == "user-123"


def test_jwt_tampered_returns_none():
    token = auth.create_access_token("user-123")
    assert auth.decode_access_token(token + "x") is None


def test_jwt_empty_returns_none():
    assert auth.decode_access_token("") is None


def test_jwt_expired_returns_none():
    from datetime import datetime, timedelta, timezone
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    token = auth.create_access_token("user-123", now=long_ago)
    assert auth.decode_access_token(token) is None


# ── endpoints (cloud mode) ──────────────────────────────────────────────────

@pytest.fixture
def cloud_client(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}"
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
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


def test_register_then_me(cloud_client):
    reg = cloud_client.post("/api/auth/register",
                            json={"email": "a@example.com", "password": "password123"})
    assert reg.status_code == 200
    token = reg.json()["access_token"]

    me = cloud_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "a@example.com"
    assert body["is_local"] is False      # a registered cloud user is neither
    assert body["is_admin"] is False      # local nor admin by default


def test_register_stores_argon2_hash_not_plaintext(cloud_client):
    cloud_client.post("/api/auth/register",
                      json={"email": "b@example.com", "password": "password123"})
    # Read the row back and confirm the password is hashed, not stored raw.
    from sqlalchemy import select
    from models.database import User

    async def _hash():
        async with app.state.sessionmaker() as s:
            u = (await s.execute(select(User).where(User.email == "b@example.com"))).scalar_one()
            return u.password_hash
    h = asyncio.run(_hash())
    assert h and h.startswith("$argon2")
    assert "password123" not in h


def test_register_duplicate_email_409(cloud_client):
    body = {"email": "dup@example.com", "password": "password123"}
    assert cloud_client.post("/api/auth/register", json=body).status_code == 200
    assert cloud_client.post("/api/auth/register", json=body).status_code == 409


def test_login_ok_and_wrong_password(cloud_client):
    cloud_client.post("/api/auth/register",
                      json={"email": "c@example.com", "password": "password123"})
    ok = cloud_client.post("/api/auth/login",
                           json={"email": "c@example.com", "password": "password123"})
    assert ok.status_code == 200 and ok.json()["access_token"]

    bad = cloud_client.post("/api/auth/login",
                            json={"email": "c@example.com", "password": "nope"})
    assert bad.status_code == 401


def test_me_without_token_401_in_cloud(cloud_client):
    assert cloud_client.get("/api/auth/me").status_code == 401


def test_short_password_rejected(cloud_client):
    r = cloud_client.post("/api/auth/register",
                          json={"email": "d@example.com", "password": "short"})
    assert r.status_code == 422      # min_length=8


# ── account_type (Business Phase 0: two doors, one engine) ───────────────────

def test_register_defaults_to_creator(cloud_client):
    cloud_client.post("/api/auth/register",
                      json={"email": "def@example.com", "password": "password123"})
    me = _login_me(cloud_client, "def@example.com")
    assert me["account_type"] == "creator"


def test_register_business_account_type_persists(cloud_client):
    cloud_client.post("/api/auth/register",
                      json={"email": "biz@example.com", "password": "password123",
                            "account_type": "business"})
    me = _login_me(cloud_client, "biz@example.com")
    assert me["account_type"] == "business"


def test_register_agency_account_type_persists(cloud_client):
    """The third door (UX phase 2.3): someone running several client brands.
    Backend-only this phase — the SPA still lands them in the creator shell."""
    cloud_client.post("/api/auth/register",
                      json={"email": "agency@example.com", "password": "password123",
                            "account_type": "agency"})
    me = _login_me(cloud_client, "agency@example.com")
    assert me["account_type"] == "agency"


def test_register_garbage_account_type_falls_back_to_creator(cloud_client):
    """A stray/garbage account_type must not 422 the registration — it degrades
    to 'creator' so a bad query-param never blocks sign-up."""
    reg = cloud_client.post("/api/auth/register",
                            json={"email": "junk@example.com", "password": "password123",
                                  "account_type": "hacker"})
    assert reg.status_code == 200
    me = _login_me(cloud_client, "junk@example.com")
    assert me["account_type"] == "creator"


def _login_me(client, email):
    tok = client.post("/api/auth/login",
                      json={"email": email, "password": "password123"}).json()["access_token"]
    return client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
