"""Brand profile: per-user niche/audience/brand storage + API + resolver."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, User as UserModel
from services.user_settings import resolve_user_profile


# ── resolver (pure) ──────────────────────────────────────────────────────────

def test_resolve_profile_none_user():
    assert resolve_user_profile(None) == {
        "niche": None, "target_audience": None, "brand_name": None,
    }


def test_resolve_profile_reads_user():
    u = SimpleNamespace(niche="Bakery", target_audience="Home bakers", brand_name="Crumb")
    assert resolve_user_profile(u) == {
        "niche": "Bakery", "target_audience": "Home bakers", "brand_name": "Crumb",
    }


# ── API round-trip (cloud) ───────────────────────────────────────────────────

@pytest.fixture
def cloud_client(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bp.db'}")

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
    c = TestClient(app)
    c.SM = SM
    yield c
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


def _reg(c):
    return c.post("/api/auth/register",
                  json={"email": "p@example.com", "password": "password123"}).json()["access_token"]


def test_profile_defaults_empty_then_saves(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    g = cloud_client.get("/api/settings/profile", headers=h)
    assert g.status_code == 200
    assert g.json() == {"niche": "", "target_audience": "", "brand_name": ""}

    cloud_client.put("/api/settings/profile", headers=h,
                     json={"niche": "Artisan bakery", "target_audience": "Home bakers",
                           "brand_name": "Crumb & Co"})
    body = cloud_client.get("/api/settings/profile", headers=h).json()
    assert body["niche"] == "Artisan bakery"
    assert body["target_audience"] == "Home bakers"
    assert body["brand_name"] == "Crumb & Co"


def test_profile_blank_clears(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    cloud_client.put("/api/settings/profile", headers=h, json={"niche": "Bakery"})
    cloud_client.put("/api/settings/profile", headers=h, json={"niche": ""})
    assert cloud_client.get("/api/settings/profile", headers=h).json()["niche"] == ""


def test_slide_style_defaults_then_saves(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    g = cloud_client.get("/api/settings/slide-style", headers=h)
    assert g.status_code == 200
    body = g.json()
    assert body["accent_color"] == "" and body["text_box_color"] == ""
    assert body["default_accent_color"].startswith("#")
    assert len(body["palette"]) >= 1                 # suggested swatches for the UI

    cloud_client.put("/api/settings/slide-style", headers=h,
                     json={"accent_color": "#123456", "text_box_color": "#abcdef"})
    saved = cloud_client.get("/api/settings/slide-style", headers=h).json()
    assert saved["accent_color"] == "#123456"
    assert saved["text_box_color"] == "#abcdef"


def test_slide_style_accepts_off_palette_and_rejects_malformed(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    # any valid hex is fine — colours are per-tenant, not a fixed palette
    assert cloud_client.put("/api/settings/slide-style", headers=h,
                            json={"accent_color": "#0f9d58"}).status_code == 200
    assert cloud_client.put("/api/settings/slide-style", headers=h,
                            json={"accent_color": "royalblue"}).status_code == 422


def test_slide_style_blank_resets_to_default(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    cloud_client.put("/api/settings/slide-style", headers=h, json={"accent_color": "#123456"})
    cloud_client.put("/api/settings/slide-style", headers=h, json={"accent_color": ""})
    assert cloud_client.get("/api/settings/slide-style", headers=h).json()["accent_color"] == ""


def test_apply_brand_slide_style_overlays_colors():
    from services.brand_engine import BrandConfig
    from services.user_settings import apply_brand_slide_style

    cfg = apply_brand_slide_style(BrandConfig(), None)          # no brand → untouched
    assert cfg.niche_box_color == BrandConfig().niche_box_color

    b = SimpleNamespace(slide_accent_color="#123456", slide_text_box_color="#abcdef")
    cfg = apply_brand_slide_style(BrandConfig(), b)
    assert cfg.niche_box_color == "#123456"
    assert cfg.desc_box_color == "#abcdef"

    unset = SimpleNamespace(slide_accent_color=None, slide_text_box_color=None)
    cfg = apply_brand_slide_style(BrandConfig(), unset)         # unset → platform default
    assert cfg.niche_box_color == BrandConfig().niche_box_color


def test_profile_persists_on_the_primary_profile(cloud_client):
    """/api/settings/* edits the profile now. The User column is written too, as
    a rollback snapshot, but it is no longer where the value lives."""
    from models.database import ManagedAccount

    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    cloud_client.put("/api/settings/profile", headers=h, json={"niche": "Coffee roasting"})

    async def _read():
        async with cloud_client.SM() as s:
            u = (await s.execute(
                select(UserModel).where(UserModel.email == "p@example.com"))).scalar_one()
            acct = (await s.execute(select(ManagedAccount).where(
                ManagedAccount.owner_user_id == u.id,
                ManagedAccount.is_primary.is_(True)))).scalar_one()
            return acct.niche, u.niche
    assert asyncio.run(_read()) == ("Coffee roasting", "Coffee roasting")


def test_settings_edit_the_primary_even_while_a_client_brand_is_active(cloud_client):
    """Two screens, two objects: Account → Brand profile is "you",
    /api/accounts/{id} is "this client". If settings followed the active brand,
    an agency would overwrite a client's niche from their own account page, and
    the User snapshot would hold whichever client was last selected — useless
    for a rollback."""
    from models.database import ManagedAccount

    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    aid = cloud_client.post("/api/accounts", headers=h,
                            json={"name": "Client A"}).json()["id"]
    cloud_client.put(f"/api/accounts/{aid}", headers=h, json={"niche": "Client niche"})
    cloud_client.post("/api/accounts/switch", headers=h, json={"account_id": aid})

    cloud_client.put("/api/settings/profile", headers=h, json={"niche": "My own niche"})

    async def _read():
        async with cloud_client.SM() as s:
            u = (await s.execute(
                select(UserModel).where(UserModel.email == "p@example.com"))).scalar_one()
            primary = (await s.execute(select(ManagedAccount).where(
                ManagedAccount.owner_user_id == u.id,
                ManagedAccount.is_primary.is_(True)))).scalar_one()
            client = await s.get(ManagedAccount, aid)
            return primary.niche, client.niche, u.niche
    primary_niche, client_niche, snapshot = asyncio.run(_read())
    assert primary_niche == "My own niche"
    assert client_niche == "Client niche"        # untouched
    assert snapshot == "My own niche"            # the snapshot follows the primary


def test_editing_a_client_brand_leaves_the_user_snapshot_alone(cloud_client):
    """The other half: only the primary mirrors. Mirror a client and the
    rollback snapshot silently becomes someone else's brand."""
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    cloud_client.put("/api/settings/profile", headers=h, json={"niche": "My own niche"})
    aid = cloud_client.post("/api/accounts", headers=h,
                            json={"name": "Client A"}).json()["id"]
    cloud_client.put(f"/api/accounts/{aid}", headers=h, json={"niche": "Client niche"})

    async def _read():
        async with cloud_client.SM() as s:
            return (await s.execute(
                select(UserModel).where(UserModel.email == "p@example.com"))).scalar_one().niche
    assert asyncio.run(_read()) == "My own niche"


def test_settings_read_back_what_the_profile_holds(cloud_client):
    """Mutation guard: the GETs must move too, or the Account screen shows the
    stale snapshot and every edit looks like it didn't save."""
    from models.database import ManagedAccount

    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    cloud_client.put("/api/settings/profile", headers=h, json={"niche": "Coffee roasting"})

    async def _poison():
        async with cloud_client.SM() as s:
            u = (await s.execute(
                select(UserModel).where(UserModel.email == "p@example.com"))).scalar_one()
            u.niche = "STALE"
            u.slide_accent_color = "#ff0000"
            u.brand_voice_preset = "bold"
            acct = (await s.execute(select(ManagedAccount).where(
                ManagedAccount.owner_user_id == u.id,
                ManagedAccount.is_primary.is_(True)))).scalar_one()
            acct.slide_accent_color = "#0a2540"
            acct.brand_voice_preset = "friendly"
            await s.commit()
    asyncio.run(_poison())

    assert cloud_client.get("/api/settings/profile", headers=h).json()["niche"] == "Coffee roasting"
    assert cloud_client.get("/api/settings/slide-style", headers=h).json()["accent_color"] == "#0a2540"
    assert cloud_client.get("/api/settings/brand-voice", headers=h).json()["preset"] == "friendly"


# ── X plan (PART XXIV) ──────────────────────────────────────────────────────

def test_x_settings_default_off_then_saves(cloud_client):
    """The premium flag is per-tenant state; the composer gates 'Long post' on it."""
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    body = cloud_client.get("/api/settings/x", headers=h).json()
    assert body["x_premium"] is False
    assert body["tweet_char_limit"] == 250     # UI shows the same budget it enforces
    assert body["max_thread_tweets"] == 15

    assert cloud_client.put("/api/settings/x", headers=h,
                            json={"x_premium": True}).status_code == 200
    assert cloud_client.get("/api/settings/x", headers=h).json()["x_premium"] is True


def test_x_settings_is_per_user(cloud_client):
    """One tenant turning Premium on must not unlock it for anyone else."""
    def token(email):
        return cloud_client.post("/api/auth/register",
                                 json={"email": email, "password": "password123"}
                                 ).json()["access_token"]
    h1 = {"Authorization": f"Bearer {token('x1@example.com')}"}
    h2 = {"Authorization": f"Bearer {token('x2@example.com')}"}
    cloud_client.put("/api/settings/x", headers=h1, json={"x_premium": True})
    assert cloud_client.get("/api/settings/x", headers=h2).json()["x_premium"] is False


def test_x_settings_requires_auth(cloud_client):
    assert cloud_client.get("/api/settings/x").status_code in (401, 403)


# ── brand logo resolver (PART XXX) ──────────────────────────────────────────

def test_apply_brand_slide_style_sets_logo_for_a_cloud_tenant():
    from pathlib import Path
    from services.brand_engine import BrandConfig
    from services.user_settings import apply_brand_slide_style

    b = SimpleNamespace(slide_accent_color=None, slide_text_box_color=None,
                        logo_path="/data/logos/u1.png")
    cfg = apply_brand_slide_style(BrandConfig(), b)
    assert cfg.logo_path == Path("/data/logos/u1.png")


def test_cloud_tenant_without_a_logo_gets_no_platform_logo():
    """A tenant's own logo, or none — the platform default must never leak.

    Deliberately called WITHOUT is_local, pinning the default's direction: a
    caller who forgets the flag must get the strict cloud posture, never the
    one that lets the platform logo through.
    """
    from pathlib import Path
    from services.brand_engine import BrandConfig
    from services.user_settings import apply_brand_slide_style

    cfg = BrandConfig(logo_path=Path("/platform/default_logo.png"))   # inherited default
    b = SimpleNamespace(slide_accent_color=None, slide_text_box_color=None,
                        logo_path=None)
    cfg = apply_brand_slide_style(cfg, b)
    assert cfg.logo_path is None


def test_the_desktop_keeps_the_config_logo():
    """is_local is a property of the deployment, not of a brand, so it arrives
    as an argument. It used to be read off the object by duck typing, which
    quietly returned False for a profile — harmless while only the User was
    ever passed, and wrong the moment the desktop owner got a profile of its
    own (UX phase 2). The namespace below has no is_local attribute at all,
    exactly like a real ManagedAccount."""
    from pathlib import Path
    from services.brand_engine import BrandConfig
    from services.user_settings import apply_brand_slide_style

    cfg = BrandConfig(logo_path=Path("/desktop/logo.png"))
    b = SimpleNamespace(slide_accent_color=None, slide_text_box_color=None,
                        logo_path=None)
    cfg = apply_brand_slide_style(cfg, b, is_local=True)
    assert cfg.logo_path == Path("/desktop/logo.png")


# ── brand logo API round-trip ───────────────────────────────────────────────

@pytest.fixture
def logo_root(tmp_path, monkeypatch):
    from services import logo_store
    root = tmp_path / "logos"
    monkeypatch.setattr(logo_store, "LOGO_ROOT", root)
    return root


def _png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(buf, format="PNG")
    return buf.getvalue()


def _token(c, email):
    return c.post("/api/auth/register",
                  json={"email": email, "password": "password123"}).json()["access_token"]


def test_logo_upload_get_and_delete(cloud_client, logo_root):
    h = {"Authorization": f"Bearer {_token(cloud_client, 'logo@example.com')}"}
    assert cloud_client.get("/api/settings/logo", headers=h).json() == {"set": False, "url": None}

    up = cloud_client.post("/api/settings/logo", headers=h,
                           files={"file": ("logo.png", _png(), "image/png")})
    assert up.status_code == 200 and up.json()["set"] is True

    got = cloud_client.get("/api/settings/logo", headers=h).json()
    assert got["set"] is True and got["url"] == "/api/settings/logo/image"
    img = cloud_client.get("/api/settings/logo/image", headers=h)
    assert img.status_code == 200 and img.content == _png()

    cloud_client.delete("/api/settings/logo", headers=h)
    assert cloud_client.get("/api/settings/logo", headers=h).json()["set"] is False
    assert cloud_client.get("/api/settings/logo/image", headers=h).status_code == 404


def test_logo_rejects_non_image(cloud_client, logo_root):
    h = {"Authorization": f"Bearer {_token(cloud_client, 'logo2@example.com')}"}
    res = cloud_client.post("/api/settings/logo", headers=h,
                            files={"file": ("x.txt", b"hello", "text/plain")})
    assert res.status_code == 415


def test_one_tenant_cannot_see_anothers_logo(cloud_client, logo_root):
    ha = {"Authorization": f"Bearer {_token(cloud_client, 'a@example.com')}"}
    hb = {"Authorization": f"Bearer {_token(cloud_client, 'b@example.com')}"}
    cloud_client.post("/api/settings/logo", headers=ha,
                      files={"file": ("logo.png", _png(), "image/png")})
    # B has uploaded nothing — must not receive A's logo.
    assert cloud_client.get("/api/settings/logo", headers=hb).json()["set"] is False
    assert cloud_client.get("/api/settings/logo/image", headers=hb).status_code == 404


# ── saved composer presets (PART XXXII) ─────────────────────────────────────

def _preset(name, **over):
    base = {"name": name, "format": "carousel_5", "tone": "educational",
            "length_tier": "sweet_spot", "default_image_source": "stock",
            "platform": "instagram", "template_style": "branded_card",
            "apply_branding": True, "show_logo": True}
    base.update(over)
    return base


def test_presets_default_empty_then_saves(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    assert cloud_client.get("/api/settings/presets", headers=h).json() == {"presets": []}

    cloud_client.put("/api/settings/presets", headers=h,
                     json={"presets": [_preset("Weekly carousel")]})
    got = cloud_client.get("/api/settings/presets", headers=h).json()["presets"]
    assert len(got) == 1
    assert got[0]["name"] == "Weekly carousel" and got[0]["format"] == "carousel_5"


def test_presets_reject_bad_enum(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    assert cloud_client.put("/api/settings/presets", headers=h,
                            json={"presets": [_preset("x", format="carousel_99")]}).status_code == 422
    assert cloud_client.put("/api/settings/presets", headers=h,
                            json={"presets": [_preset("x", length_tier="epic")]}).status_code == 422


def test_presets_reject_upload_source(cloud_client):
    """A preset stores settings, not files — 'my photos' can't be its source."""
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    assert cloud_client.put("/api/settings/presets", headers=h,
                            json={"presets": [_preset("x", default_image_source="upload")]}
                            ).status_code == 422


def test_presets_cap_at_twenty(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    many = {"presets": [_preset(f"p{i}") for i in range(21)]}
    assert cloud_client.put("/api/settings/presets", headers=h, json=many).status_code == 422


def test_presets_dedupe_by_name(cloud_client):
    h = {"Authorization": f"Bearer {_reg(cloud_client)}"}
    cloud_client.put("/api/settings/presets", headers=h, json={"presets": [
        _preset("Same", format="single"), _preset("Same", format="carousel_3")]})
    got = cloud_client.get("/api/settings/presets", headers=h).json()["presets"]
    assert len(got) == 1 and got[0]["format"] == "carousel_3"   # last one wins


def test_presets_are_per_user(cloud_client):
    def token(email):
        return cloud_client.post("/api/auth/register",
                                 json={"email": email, "password": "password123"}
                                 ).json()["access_token"]
    ha = {"Authorization": f"Bearer {token('pa@example.com')}"}
    hb = {"Authorization": f"Bearer {token('pb@example.com')}"}
    cloud_client.put("/api/settings/presets", headers=ha, json={"presets": [_preset("Mine")]})
    assert cloud_client.get("/api/settings/presets", headers=hb).json() == {"presets": []}
