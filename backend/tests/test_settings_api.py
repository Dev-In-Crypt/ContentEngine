"""Per-user credential vault: PUT/GET /api/settings/credentials + effective settings."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import build_settings_for_user, get_db, get_settings
from config import Settings
from main import app
from models.database import Base, User, UserCredentials


@pytest.fixture
def cloud_client(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'set.db'}"
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


def _register(client, email="v@example.com"):
    token = client.post("/api/auth/register",
                        json={"email": email, "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_put_stores_encrypted_not_plaintext(cloud_client):
    hdr = _register(cloud_client)
    r = cloud_client.put("/api/settings/credentials",
                         json={"openrouter_api_key": "sk-or-secret-xyz"}, headers=hdr)
    assert r.status_code == 200

    async def _stored():
        async with app.state.sessionmaker() as s:
            uid = (await s.execute(select(User.id))).scalars().first()
            creds = await s.get(UserCredentials, uid)
            return creds.openrouter_api_key_enc
    enc = asyncio.run(_stored())
    assert enc and "sk-or-secret-xyz" not in enc      # encrypted at rest


def test_get_never_returns_raw_value(cloud_client):
    hdr = _register(cloud_client)
    cloud_client.put("/api/settings/credentials",
                     json={"openrouter_api_key": "sk-or-secret-xyz"}, headers=hdr)
    body = cloud_client.get("/api/settings/credentials", headers=hdr).json()
    assert body["openrouter_api_key"]["set"] is True
    assert "sk-or-secret-xyz" not in str(body)         # only a mask, never the value
    assert body["openrouter_api_key"]["masked"] == "••••-xyz"
    assert body["imgbb_api_key"]["set"] is False       # untouched key reports unset


def test_effective_settings_overlays_user_key(cloud_client):
    hdr = _register(cloud_client, email="eff@example.com")
    cloud_client.put("/api/settings/credentials",
                     json={"openrouter_api_key": "user-own-key"}, headers=hdr)

    async def _eff():
        async with app.state.sessionmaker() as s:
            user = (await s.execute(
                select(User).where(User.email == "eff@example.com")
            )).scalar_one()
            return await build_settings_for_user(s, user)
    settings = asyncio.run(_eff())
    assert settings.openrouter_api_key == "user-own-key"


def test_kling_key_round_trips_through_put_and_get(cloud_client):
    """A field present in _CRED_FIELDS but missing from CredentialsUpdate raises
    AttributeError at request time — that gap is exactly what this pins."""
    hdr = _register(cloud_client)
    r = cloud_client.put("/api/settings/credentials",
                         json={"kling_api_key": "kling-secret-abc"}, headers=hdr)
    assert r.status_code == 200
    body = cloud_client.get("/api/settings/credentials", headers=hdr).json()
    assert body["kling_api_key"]["set"] is True
    assert "kling-secret-abc" not in str(body)


def test_local_user_gets_platform_settings(cloud_client):
    # A local user must NOT be overlaid — it uses the platform .env as-is.
    async def _eff():
        async with app.state.sessionmaker() as s:
            local = User(email="local@localhost", is_local=True)
            s.add(local)
            await s.commit()
            eff = await build_settings_for_user(s, local)
            platform = Settings()
            return eff.openrouter_api_key, platform.openrouter_api_key
    eff_key, platform_key = asyncio.run(_eff())
    assert eff_key == platform_key


def test_credentials_require_auth_in_cloud(cloud_client):
    assert cloud_client.get("/api/settings/credentials").status_code == 401
