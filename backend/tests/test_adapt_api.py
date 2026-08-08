"""One idea, a second network: POST /api/posts/{id}/adapt/{platform}.

The route takes an existing post and writes a sibling — a real `Post` row in the
same variant group, generated for a different network. Siblings are ordinary
posts on purpose, so publishing, scheduling, approval and analytics all keep
working on them without a line changed.

Almost all of the risk here is in the guards rather than the generation, and
each fails differently:

  * ownership — 404 for someone else's post, never 403,
  * an allow-list of networks, because a LinkedIn sibling could never be
    published and would sit in the Queue forever,
  * a refusal for Business drafts, whose `claim_check` verdict describes the
    text that is about to be replaced,
  * idempotency, so a second click returns the first sibling instead of
    spending a second generation,
  * and the brand coming from the POST rather than from whichever client the
    user happens to be working in.

`caption_gen.generate` is stubbed, never `text_provider`. The generator is not
deterministic, retries once on unparseable JSON, and X modes fire extra
`shorten_text` calls — so any assertion counting provider calls would flake.
Counting calls to `generate` is stable and is what the cost question is actually
about.
"""
import asyncio
import io
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_content_engine, get_db, get_settings
from config import Settings
from main import app
from models.database import (
    Base, ManagedAccount, Post as PostModel, Slide as SlideModel, User,
    Workspace,
)
from services.auth import hash_password
from services.caption_generator import GeneratedCaption
from services.content_engine import ContentEngine
from services.free_generation import FREE_POST_LIMIT
from services.user_settings import _CRED_FIELDS

UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads" / "posts"

TEXT_MODEL = "test/text-model"


def _platform(db_url: str, **overrides) -> Settings:
    """Every credential blank unless a test says otherwise.

    Settings() reads the developer's real backend/.env. Leaving it to do that
    would give the PLATFORM an OpenRouter key on one machine and not on another,
    which since UX phase 6.3 decides whether an account without its own key
    adapts for free or is refused — a test that passes at home and fails in CI.
    """
    fields = {field: "" for field in _CRED_FIELDS}
    fields.update(database_url=db_url, api_token="", app_mode="cloud",
                  default_text_provider="openrouter", default_text_model=TEXT_MODEL)
    fields.update(overrides)
    return Settings(**fields)


def _jpeg(color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (120, 150), color).save(buf, format="JPEG")
    return buf.getvalue()


def _caption(**over) -> GeneratedCaption:
    fields = dict(
        caption="Adapted for the other network.",
        hashtags=["#adapted"], cta="Read on.", hook="A sharper hook.",
        image_search_queries=[], image_gen_prompts=[],
        alt_text="An adapted image.", seo_keywords=["adapted"],
        slide_overlays=["NEW OVERLAY"], thread_parts=[], sources=[],
    )
    fields.update(over)
    return GeneratedCaption(**fields)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'adapt.db'}"


@pytest.fixture
def sm(db_url):
    eng = create_async_engine(db_url)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def client(db_url, sm):
    fake_engine = AsyncMock(spec=ContentEngine)
    fake_engine.caption_gen = AsyncMock()
    fake_engine.caption_gen.generate.return_value = _caption()

    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_content_engine] = lambda: fake_engine
    # app_mode="cloud" is load-bearing: the local desktop owner is exempt from
    # every ownership gate, so without it the isolation test would pass by
    # asserting nothing.
    app.dependency_overrides[get_settings] = lambda: _platform(db_url)
    app.state.sessionmaker = sm

    tc = TestClient(app)
    tc.fake_engine = fake_engine
    yield tc

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_content_engine, None)
    app.dependency_overrides.pop(get_settings, None)


def _register(client, email: str) -> dict:
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _user_id(sm, email: str) -> str:
    async def _go():
        async with sm() as db:
            return (await db.execute(
                select(User).where(User.email == email))).scalar_one().id
    return asyncio.run(_go())


def _seed_post(sm, user_id: str, *, slides: int = 1, platform: str = "instagram",
               account_id=None, workspace_id=None, raw: bool = True,
               tone: str | None = "casual", **over) -> str:
    """A post with real slide files on disk, as the generator would have left it."""
    post_id = str(uuid.uuid4())
    fields = dict(
        id=post_id, user_id=user_id, topic="Sourdough starter",
        format="carousel_3" if slides > 1 else "single", status="preview",
        caption="The original caption.", hashtags=["#bread"],
        cta="Save this.", hook="Your starter is fine.", platform=platform,
        template_style="branded_card", text_model=TEXT_MODEL,
        variant_group_id=post_id, tone=tone,
        managed_account_id=account_id, workspace_id=workspace_id,
    )
    fields.update(over)

    async def _go():
        async with sm() as db:
            db.add(PostModel(**fields))
            for n in range(1, slides + 1):
                d = UPLOADS_DIR / post_id
                d.mkdir(parents=True, exist_ok=True)
                (d / f"slide_{n}.jpg").write_bytes(_jpeg())
                raw_path = None
                if raw:
                    (d / f"slide_{n}_raw.jpg").write_bytes(_jpeg("blue"))
                    raw_path = str(d / f"slide_{n}_raw.jpg")
                db.add(SlideModel(
                    post_id=post_id, slide_number=n, image_source="stock",
                    image_path=str(d / f"slide_{n}.jpg"), raw_image_path=raw_path,
                    search_query="bread", original_overlay_text=f"OLD {n}",
                    original_niche_text="Baking",
                    render_params={"template_style": "branded_card",
                                   "overlay_text": f"OLD {n}", "niche_text": "Baking"},
                ))
            await db.commit()
    asyncio.run(_go())
    return post_id


def _rows(sm, model, clause):
    async def _go():
        async with sm() as db:
            return (await db.execute(select(model).where(clause))).scalars().all()
    return asyncio.run(_go())


def _group(sm, group_id):
    return _rows(sm, PostModel, PostModel.variant_group_id == group_id)


@pytest.fixture
def owner(client, sm):
    """A registered user with their own key and one Instagram post to adapt.

    The key matters since UX phase 6.3: an account without one adapts on the
    free allowance instead, which is a different path with its own counter. This
    file is about adaptation, so its owner pays their own way; the free path has
    its own tests at the bottom.
    """
    headers = _register(client, "owner@ex.com")
    client.put("/api/settings/credentials",
               json={"openrouter_api_key": "their-own-key"}, headers=headers)
    client.put("/api/settings/ai",
               json={"text_provider": "openrouter", "text_model": TEXT_MODEL},
               headers=headers)
    uid = _user_id(sm, "owner@ex.com")
    return {"headers": headers, "user_id": uid,
            "post_id": _seed_post(sm, uid)}


# ── guards ──────────────────────────────────────────────────────────────────

def test_another_users_post_is_not_found(client, sm, owner):
    other = _register(client, "other@ex.com")
    r = client.post(f"/api/posts/{owner['post_id']}/adapt/x", headers=other)
    # 404 and not 403: confirming the post exists tells a stranger it exists.
    assert r.status_code == 404
    assert client.fake_engine.caption_gen.generate.call_count == 0


def test_an_unknown_network_is_rejected(client, owner):
    r = client.post(f"/api/posts/{owner['post_id']}/adapt/tiktok",
                    headers=owner["headers"])
    assert r.status_code == 422


def test_linkedin_is_refused_because_it_cannot_be_published(client, owner):
    """LinkedIn generates but has no publisher, so a LinkedIn sibling is a row
    that could never leave the Queue."""
    r = client.post(f"/api/posts/{owner['post_id']}/adapt/linkedin",
                    headers=owner["headers"])
    assert r.status_code == 422
    assert client.fake_engine.caption_gen.generate.call_count == 0


def test_a_business_draft_cannot_be_adapted(client, sm, owner):
    """Its claim_check is an LLM verdict about the text that is about to be
    replaced. Copying it attributes the verdict to words nobody checked;
    dropping it lets a Business account approve an unchecked draft."""
    async def _ws():
        async with sm() as db:
            ws = Workspace(owner_user_id=owner["user_id"], name="Acme")
            db.add(ws)
            await db.commit()
            return ws.id
    ws_id = asyncio.run(_ws())
    biz = _seed_post(sm, owner["user_id"], workspace_id=ws_id)

    r = client.post(f"/api/posts/{biz}/adapt/x", headers=owner["headers"])
    assert r.status_code == 422
    assert client.fake_engine.caption_gen.generate.call_count == 0


def test_no_text_model_is_a_400(client, sm, owner):
    """Before any spend, and with the same wording /generate uses."""
    app.dependency_overrides[get_settings] = lambda: _platform(
        "sqlite+aiosqlite:///:memory:", default_text_model="")
    client.put("/api/settings/ai", json={"text_model": ""},
               headers=owner["headers"])
    no_model = _seed_post(sm, owner["user_id"], text_model=None)

    r = client.post(f"/api/posts/{no_model}/adapt/x", headers=owner["headers"])
    assert r.status_code == 400
    assert client.fake_engine.caption_gen.generate.call_count == 0


# ── idempotency ─────────────────────────────────────────────────────────────

def test_adapting_twice_returns_the_same_sibling_and_generates_once(client, sm, owner):
    first = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                        headers=owner["headers"])
    assert first.status_code == 200, first.text
    second = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                         headers=owner["headers"])
    assert second.status_code == 200

    assert first.json()["id"] == second.json()["id"]
    assert client.fake_engine.caption_gen.generate.call_count == 1
    assert len(_group(sm, owner["post_id"])) == 2


def test_adapting_to_its_own_network_returns_the_post_itself(client, owner):
    """Falls out of the same lookup rather than needing its own branch."""
    r = client.post(f"/api/posts/{owner['post_id']}/adapt/instagram",
                    headers=owner["headers"])
    assert r.status_code == 200
    assert r.json()["id"] == owner["post_id"]
    assert client.fake_engine.caption_gen.generate.call_count == 0


def test_the_sibling_can_be_reached_from_either_end(client, sm, owner):
    """Adapting the sibling back returns the original — the group is a set, not
    a chain, so there is no 'original' to be lost."""
    sibling = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                          headers=owner["headers"]).json()["id"]
    back = client.post(f"/api/posts/{sibling}/adapt/instagram",
                       headers=owner["headers"])
    assert back.status_code == 200
    assert back.json()["id"] == owner["post_id"]


# ── what the sibling is ─────────────────────────────────────────────────────

def test_the_sibling_joins_the_group_on_the_new_network(client, sm, owner):
    r = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                    headers=owner["headers"])
    body = r.json()
    assert body["platform"] == "x"
    assert body["variant_group_id"] == owner["post_id"]
    assert body["caption"] == "Adapted for the other network."
    assert {p.platform for p in _group(sm, owner["post_id"])} == {"instagram", "x"}


def test_the_sibling_is_a_fresh_preview(client, sm, owner):
    """It has never been anywhere. Carrying the source's published state over
    would make the editor claim a post is live that was written a second ago."""
    async def _publish():
        async with sm() as db:
            p = await db.get(PostModel, owner["post_id"])
            p.status = "published"
            p.instagram_media_id = "ig-123"
            p.published_url = "https://example.com/p/1"
            p.publish_attempts = 3
            await db.commit()
    asyncio.run(_publish())

    sid = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                      headers=owner["headers"]).json()["id"]
    sib = _rows(sm, PostModel, PostModel.id == sid)[0]
    assert sib.status == "preview"
    assert sib.instagram_media_id is None
    assert sib.published_url is None
    assert sib.published_at is None
    assert sib.publish_attempts == 0
    assert sib.claim_check is None
    assert sib.video_path is None


def test_the_sibling_keeps_the_tone_the_source_was_written_in(client, owner):
    client.post(f"/api/posts/{owner['post_id']}/adapt/x", headers=owner["headers"])
    assert client.fake_engine.caption_gen.generate.call_args.kwargs["tone"] == "casual"


def test_the_sibling_uses_the_source_posts_brand(client, sm, owner):
    """An agency adapting a Client A post while working in Client B must get A's
    voice. Resolving the ACTIVE brand here would be a fresh instance of the bug
    UX phase 2 was written to fix."""
    async def _two_brands():
        async with sm() as db:
            a = ManagedAccount(owner_user_id=owner["user_id"], name="Client A",
                               brand_voice_preset="playful", niche="Bakery")
            b = ManagedAccount(owner_user_id=owner["user_id"], name="Client B",
                               brand_voice_preset="authoritative", niche="Law")
            db.add_all([a, b])
            await db.flush()
            u = await db.get(User, owner["user_id"])
            u.active_account_id = b.id          # working in B...
            await db.commit()
            return a.id
    a_id = asyncio.run(_two_brands())
    post = _seed_post(sm, owner["user_id"], account_id=a_id)   # ...post belongs to A

    client.post(f"/api/posts/{post}/adapt/x", headers=owner["headers"])
    kwargs = client.fake_engine.caption_gen.generate.call_args.kwargs
    assert kwargs["niche"] == "Bakery"


# ── slides and images ───────────────────────────────────────────────────────

def test_the_siblings_slide_files_are_its_own(client, sm, owner):
    """Sharing image_path looks like reuse but an overlay edit on either post
    writes in place — it would silently rewrite the other network's picture, and
    deleting either post would orphan the other's pixels."""
    sid = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                      headers=owner["headers"]).json()["id"]
    src = _rows(sm, SlideModel, SlideModel.post_id == owner["post_id"])[0]
    sib = _rows(sm, SlideModel, SlideModel.post_id == sid)[0]

    assert sib.image_path != src.image_path
    assert Path(sib.image_path).exists()
    assert str(sid) in sib.image_path


def test_the_siblings_image_carries_the_new_overlay(client, sm, owner):
    """The picture is re-rendered from the stored raw with the overlay the NEW
    caption produced. Copying the JPEG as-is would put Instagram wording, written
    for a caption that no longer exists, on the X post's image."""
    sid = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                      headers=owner["headers"]).json()["id"]
    sib = _rows(sm, SlideModel, SlideModel.post_id == sid)[0]
    assert sib.render_params["overlay_text"] == "NEW OVERLAY"
    assert sib.original_overlay_text == "NEW OVERLAY"


def test_an_x_sibling_of_a_carousel_is_a_single_slide(client, sm, owner):
    """X has never had more than one slide in this product — the composer
    already forces carousel to single when you switch to it."""
    carousel = _seed_post(sm, owner["user_id"], slides=3)
    sid = client.post(f"/api/posts/{carousel}/adapt/x",
                      headers=owner["headers"]).json()["id"]

    assert len(_rows(sm, SlideModel, SlideModel.post_id == sid)) == 1
    assert _rows(sm, PostModel, PostModel.id == sid)[0].format == "single"


def test_the_caption_call_is_told_the_siblings_slide_count(client, sm, owner):
    """Ask for three overlay lines on a one-slide post and two of them are
    written, paid for and thrown away."""
    carousel = _seed_post(sm, owner["user_id"], slides=3)
    client.post(f"/api/posts/{carousel}/adapt/x", headers=owner["headers"])
    assert client.fake_engine.caption_gen.generate.call_args.kwargs["num_slides"] == 1


def test_a_missing_source_file_does_not_fail_the_whole_adapt(client, sm, owner):
    """An uploads volume that wasn't persisted is a case the preview already
    tolerates; losing the text because a JPEG is gone would be worse."""
    bare = _seed_post(sm, owner["user_id"], raw=False)
    for f in (UPLOADS_DIR / bare).iterdir():
        f.unlink()

    r = client.post(f"/api/posts/{bare}/adapt/x", headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["caption"] == "Adapted for the other network."


# ── the tab bar's data ──────────────────────────────────────────────────────

def test_a_lone_post_lists_only_itself(client, owner):
    """A group of one is still a group, and the tab bar has to render something
    for it — a post nobody adapted must not come back with an empty variant
    list and lose its own tab."""
    r = client.get(f"/api/posts/{owner['post_id']}", headers=owner["headers"])
    assert [v["platform"] for v in r.json()["variants"]] == ["instagram"]


def test_the_group_is_listed_from_either_end(client, owner):
    """Whichever sibling is open, the tab bar is the same — it is a property of
    the group, not of the post you happen to be looking at."""
    sibling = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                          headers=owner["headers"]).json()

    assert sorted(v["platform"] for v in sibling["variants"]) == ["instagram", "x"]
    from_source = client.get(f"/api/posts/{owner['post_id']}",
                             headers=owner["headers"]).json()
    assert sorted(v["platform"] for v in from_source["variants"]) == ["instagram", "x"]
    assert {v["id"] for v in from_source["variants"]} == {owner["post_id"], sibling["id"]}


def test_a_variant_carries_the_status_its_tab_has_to_show(client, sm, owner):
    """The tab shows a dot per network, so the status travels with the row."""
    async def _publish():
        async with sm() as db:
            p = await db.get(PostModel, owner["post_id"])
            p.status = "published"
            await db.commit()
    asyncio.run(_publish())

    r = client.get(f"/api/posts/{owner['post_id']}", headers=owner["headers"])
    assert r.json()["variants"][0]["status"] == "published"


def test_another_users_post_never_appears_in_a_group(client, sm, owner):
    """variant_group_id is a plain uuid with no owner of its own. A row carrying
    someone else's group key must not be listed as a tab — that would put a
    stranger's post id in the response and one click from the editor."""
    other_headers = _register(client, "stranger@ex.com")
    stranger = _user_id(sm, "stranger@ex.com")
    _seed_post(sm, stranger, platform="x", variant_group_id=owner["post_id"])

    r = client.get(f"/api/posts/{owner['post_id']}", headers=owner["headers"])
    assert [v["platform"] for v in r.json()["variants"]] == ["instagram"]
    assert other_headers                      # registered, and sees nothing here


def test_editing_a_caption_does_not_claim_the_group_is_empty(client, owner):
    """Only the three endpoints the SPA binds from fill this list. PUT /caption
    returns [] — and the SPA must not read that as "the group lost its tabs",
    which is why it rebuilds the bar only when the group id changes."""
    client.post(f"/api/posts/{owner['post_id']}/adapt/x", headers=owner["headers"])
    r = client.put(f"/api/posts/{owner['post_id']}/caption",
                   headers=owner["headers"],
                   json={"caption": "Edited.", "hashtags": [], "seo_keywords": []})
    assert r.status_code == 200
    assert r.json()["variants"] == []
    assert r.json()["variant_group_id"] == owner["post_id"]


# ── failure ─────────────────────────────────────────────────────────────────

def test_a_sibling_that_appears_mid_generation_is_not_duplicated(client, sm, owner):
    """Two clicks a millisecond apart both miss the idempotency lookup and both
    pay for a generation. The work is duplicated — nothing can prevent that
    without a lock — but the ROW must not be, or the group ends up with two X
    posts and only one of them will ever be revoked.

    The concurrent request is simulated by writing the sibling from inside the
    caption call, which is exactly the window the second lookup exists to close.
    """
    rival_id = str(uuid.uuid4())

    async def _generate_and_race(**kwargs):
        async with sm() as db:
            db.add(PostModel(
                id=rival_id, user_id=owner["user_id"], topic="Sourdough starter",
                format="single", status="preview", platform="x",
                variant_group_id=owner["post_id"], caption="Got there first.",
            ))
            await db.commit()
        return _caption()

    client.fake_engine.caption_gen.generate.side_effect = _generate_and_race

    r = client.post(f"/api/posts/{owner['post_id']}/adapt/x", headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["id"] == rival_id          # the winner's row, not a second one
    assert len([p for p in _group(sm, owner["post_id"]) if p.platform == "x"]) == 1


def test_a_generation_failure_leaves_no_row_behind(client, sm, owner):
    client.fake_engine.caption_gen.generate.side_effect = RuntimeError("boom")
    r = client.post(f"/api/posts/{owner['post_id']}/adapt/x",
                    headers=owner["headers"])
    assert r.status_code == 502
    assert len(_group(sm, owner["post_id"])) == 1


# ── on our key (UX phase 6.3) ───────────────────────────────────────────────
#
# A sibling is a full caption generation, so an account with no key of its own
# spends one of its free posts on it. Anything else would mean the second
# network tab is the one place in the product where "you need a key" comes back
# — right after the phase that moved that question off the doorstep.


@pytest.fixture
def penniless(client, sm, monkeypatch):
    """A registered account that has pasted nothing, with a post to adapt.

    The platform key is set on this fixture's settings and nowhere else: it is
    what makes the free path available at all, and leaving it in the shared
    fixture would put every test in this file on it.

    `build_content_engine` is patched as well, because the free path deliberately
    does NOT use the injected engine — it builds one from the application's own
    credentials, which on an unpatched run means a real call to OpenRouter. The
    same fake comes back, so every assertion below still reads the one engine.
    """
    import api.routes.posts as posts_routes

    monkeypatch.setattr(posts_routes, "build_content_engine",
                        lambda *a, **kw: client.fake_engine)
    app.dependency_overrides[get_settings] = lambda: _platform(
        "sqlite+aiosqlite:///:memory:", openrouter_api_key="app-key",
        default_text_model="our/model")
    headers = _register(client, "broke@ex.com")
    uid = _user_id(sm, "broke@ex.com")
    return {"headers": headers, "user_id": uid, "post_id": _seed_post(sm, uid)}


def _used(sm, user_id: str) -> int:
    async def _go():
        async with sm() as db:
            return (await db.get(User, user_id)).free_generations_used
    return asyncio.run(_go())


def test_an_account_with_no_key_adapts_on_the_allowance(client, sm, penniless):
    r = client.post(f"/api/posts/{penniless['post_id']}/adapt/x",
                    headers=penniless["headers"])

    assert r.status_code == 200, r.text
    assert _used(sm, penniless["user_id"]) == 1


def test_our_key_adapts_with_our_model_not_the_source_post_s(client, sm, penniless):
    """The source may have been written on a key they have since removed, or on
    a model they chose while paying for it themselves. Neither makes it a model
    we agreed to buy."""
    on_their_old_model = _seed_post(sm, penniless["user_id"],
                                    text_model="anthropic/expensive")

    client.post(f"/api/posts/{on_their_old_model}/adapt/x",
                headers=penniless["headers"])

    kwargs = client.fake_engine.caption_gen.generate.call_args.kwargs
    assert kwargs["text_model"] == "our/model"
    assert kwargs["web_grounded"] is False


def test_reopening_a_tab_that_already_has_a_sibling_costs_nothing(client, sm, penniless):
    """The guard this sub-phase turns on. Result tabs are clicked back and forth
    while comparing two networks, and each of those clicks reaches this route —
    so the claim has to live on the far side of the idempotent lookup, or five
    free posts would last five clicks."""
    first = client.post(f"/api/posts/{penniless['post_id']}/adapt/x",
                        headers=penniless["headers"])
    assert first.status_code == 200

    for _ in range(3):
        again = client.post(f"/api/posts/{penniless['post_id']}/adapt/x",
                            headers=penniless["headers"])
        assert again.json()["id"] == first.json()["id"]

    assert _used(sm, penniless["user_id"]) == 1
    assert client.fake_engine.caption_gen.generate.call_count == 1


def test_an_exhausted_allowance_refuses_the_adaptation(client, sm, penniless):
    async def _spend_it_all():
        async with sm() as db:
            user = await db.get(User, penniless["user_id"])
            user.free_generations_used = FREE_POST_LIMIT
            await db.commit()
    asyncio.run(_spend_it_all())

    r = client.post(f"/api/posts/{penniless['post_id']}/adapt/x",
                    headers=penniless["headers"])

    assert r.status_code == 409
    assert "key" in r.json()["detail"].lower()
    assert client.fake_engine.caption_gen.generate.call_count == 0


def test_a_failed_adaptation_gives_the_allowance_back(client, sm, penniless):
    client.fake_engine.caption_gen.generate.side_effect = RuntimeError("boom")

    r = client.post(f"/api/posts/{penniless['post_id']}/adapt/x",
                    headers=penniless["headers"])

    assert r.status_code == 502
    assert _used(sm, penniless["user_id"]) == 0


def test_losing_a_race_is_not_charged_to_the_loser(client, sm, penniless):
    """Two clicks a millisecond apart both generate; only one row survives. The
    loser's caption is thrown away, so the free post it claimed bought nothing
    anybody kept — charging for it would bill the same person twice for one
    sibling."""
    rival_id = str(uuid.uuid4())

    async def _generate_and_race(**kwargs):
        async with sm() as db:
            db.add(PostModel(
                id=rival_id, user_id=penniless["user_id"], topic="Sourdough starter",
                format="single", status="preview", platform="x",
                variant_group_id=penniless["post_id"], caption="Got there first.",
            ))
            await db.commit()
        return _caption()

    client.fake_engine.caption_gen.generate.side_effect = _generate_and_race

    r = client.post(f"/api/posts/{penniless['post_id']}/adapt/x",
                    headers=penniless["headers"])

    assert r.json()["id"] == rival_id
    assert _used(sm, penniless["user_id"]) == 0
