"""A slide rendered from the brand alone, with no post behind it.

Every render in the product until now hung off a post: the routes take a
post_id, read its stored render params, and overwrite its file. So the only way
to see what a colour did to a slide was to generate a post and look at it —
which costs a model call, and on the free tier costs one of two.

The Brand kit needed a picture beside the fields. This is the route that draws
one, from whatever the account has saved and a fixed sample sentence.

The care here is about scope, not pixels: an agency has several brands, and a
preview that quietly rendered the wrong one would be worse than no preview —
you would trust it.
"""
import asyncio
import io as _io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, ManagedAccount, User as UserModel


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bp.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _seed(sm, accents):
    """One local user with a primary profile per accent given."""
    uid = str(uuid.uuid4())
    ids = []

    async def _go():
        async with sm() as s:
            s.add(UserModel(id=uid, email="local@example.com", password_hash="",
                            is_local=True, account_type="creator"))
            for i, accent in enumerate(accents):
                aid = str(uuid.uuid4())
                ids.append(aid)
                s.add(ManagedAccount(id=aid, owner_user_id=uid, name=f"Brand {i}",
                                     is_primary=(i == 0), slide_accent_color=accent))
            await s.commit()
        async with sm() as s:
            u = await s.get(UserModel, uid)
            u.active_account_id = ids[0]
            await s.commit()
    asyncio.run(_go())
    return uid, ids


@pytest.fixture
def client(sm, tmp_path):
    settings = Settings(app_mode="local", api_token="",
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bp.db'}")

    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)


async def _switch(sm, uid, aid):
    async with sm() as s:
        u = await s.get(UserModel, uid)
        u.active_account_id = aid
        await s.commit()


def test_a_preview_is_drawn_without_any_post(client, sm):
    """The whole point: seeing what a colour does used to cost a generation."""
    _seed(sm, ["#8a4b2a"])

    r = client.get("/api/settings/slide-preview")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    img = Image.open(_io.BytesIO(r.content))
    assert img.size == (1080, 1350)


def test_the_preview_follows_the_colour_the_account_saved(client, sm):
    """A preview that ignores the setting is a decoration with a loading time."""
    uid, ids = _seed(sm, ["#8a4b2a", "#2a5c8a"])
    first = client.get("/api/settings/slide-preview").content

    asyncio.run(_switch(sm, uid, ids[1]))
    second = client.get("/api/settings/slide-preview").content

    assert first != second


def test_the_preview_is_not_cached_across_brands(client, sm):
    """An agency switches brands all day. A preview served from a cache keyed on
    nothing shows the last brand looked at, and it looks entirely plausible."""
    uid, ids = _seed(sm, ["#8a4b2a", "#2a5c8a"])
    asyncio.run(_switch(sm, uid, ids[1]))
    second = client.get("/api/settings/slide-preview").content

    asyncio.run(_switch(sm, uid, ids[0]))
    back = client.get("/api/settings/slide-preview").content

    assert back != second
