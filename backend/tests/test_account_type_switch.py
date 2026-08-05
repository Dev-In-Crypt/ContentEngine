"""Product switcher: PUT /api/auth/account-type flips a signed-in account between
the Creators and Business products under one login (one email can't register
twice). The switch is the mutation target — if it stops persisting, the business
gate never opens.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base


@pytest.fixture
def client(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'atype.db'}")

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


def _register(c, email):
    r = c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_switch_creator_to_business_and_back(client):
    h = _register(client, "switch@ex.com")
    # default is creator; the business gate is shut
    assert client.get("/api/auth/me", headers=h).json()["account_type"] == "creator"
    assert client.get("/api/business/sources", headers=h).status_code == 403

    # flip to business → /me reflects it AND the gate opens (mutation guard:
    # drop the persist → this 200 goes back to 403)
    r = client.put("/api/auth/account-type", headers=h, json={"account_type": "business"})
    assert r.status_code == 200 and r.json()["account_type"] == "business"
    assert client.get("/api/auth/me", headers=h).json()["account_type"] == "business"
    assert client.get("/api/business/sources", headers=h).status_code == 200

    # flip back to creator → gate shuts again
    r = client.put("/api/auth/account-type", headers=h, json={"account_type": "creator"})
    assert r.status_code == 200 and r.json()["account_type"] == "creator"
    assert client.get("/api/business/sources", headers=h).status_code == 403


def test_switch_to_agency(client):
    h = _register(client, "agency@ex.com")
    r = client.put("/api/auth/account-type", headers=h, json={"account_type": "agency"})
    assert r.status_code == 200 and r.json()["account_type"] == "agency"
    # agency is the creator engine with several brands — not the Business product
    assert client.get("/api/business/sources", headers=h).status_code == 403


def test_unknown_type_rejected(client):
    h = _register(client, "bad@ex.com")
    assert client.put("/api/auth/account-type", headers=h,
                      json={"account_type": "wizard"}).status_code == 422
    # unchanged after a rejected switch
    assert client.get("/api/auth/me", headers=h).json()["account_type"] == "creator"


def test_switch_requires_auth(client):
    assert client.put("/api/auth/account-type", json={"account_type": "business"}).status_code == 401
