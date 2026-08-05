"""The brand profile as a first-class row (phase 2.1).

Every user owns exactly one primary profile. `ensure_primary_profile` is the
one function that creates it, and the data migration performs the identical
three writes in SQL — seed, point `active_account_id` at it, and adopt the
user's untagged posts. Identical semantics is the point: if the two drift, a
user seeded at runtime and a user seeded by the migration end up in different
states.

The post adoption is the one that matters. `list_posts` filters
`managed_account_id == active_account_id`, so the moment a profile becomes
active, every post still carrying NULL becomes invisible to its owner.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import Base, ManagedAccount, Post, User
from services.managed_account import ensure_primary_profile, mirror_primary_to_user

BRAND = {
    "niche": "artisan bakery",
    "target_audience": "weekend shoppers",
    "brand_name": "Crumb",
    "slide_accent_color": "#0a2540",
    "slide_text_box_color": "#ffffff",
    "logo_path": "/uploads/logos/u1.png",
    "brand_voice_preset": "warm",
    "brand_voice_custom": "never shout",
}


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'profile.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _run(coro_fn):
    return asyncio.run(coro_fn())


def _seed_user(sm, **kw):
    uid = str(uuid.uuid4())

    async def _s():
        async with sm() as db:
            db.add(User(id=uid, email=f"{uid}@ex.com", **kw))
            await db.commit()
    _run(_s)
    return uid


def _seed_post(sm, user_id, account_id, topic):
    async def _s():
        async with sm() as db:
            db.add(Post(id=str(uuid.uuid4()), user_id=user_id,
                        managed_account_id=account_id, topic=topic,
                        format="single", status="preview"))
            await db.commit()
    _run(_s)


def _ensure(sm, uid):
    async def _s():
        async with sm() as db:
            user = await db.get(User, uid)
            profile = await ensure_primary_profile(db, user)
            return profile.id
    return _run(_s)


def _topics_on(sm, account_id):
    async def _s():
        async with sm() as db:
            rows = (await db.execute(select(Post).where(
                Post.managed_account_id == account_id))).scalars().all()
            return {p.topic for p in rows}
    return _run(_s)


# ------------------------------------------------------------------ seeding

def test_the_profile_copies_the_users_brand(sm):
    """The migration is a rename of where identity lives, not a reset of it.
    Anything not copied is a field the user silently loses."""
    uid = _seed_user(sm, **BRAND)
    pid = _ensure(sm, uid)

    async def _check():
        async with sm() as db:
            p = await db.get(ManagedAccount, pid)
            assert p.owner_user_id == uid and p.is_primary
            for field, value in BRAND.items():
                assert getattr(p, field) == value, field
    _run(_check)


def _name_of(sm, pid):
    async def _s():
        async with sm() as db:
            return (await db.get(ManagedAccount, pid)).name
    return _run(_s)


def test_the_profile_takes_its_name_from_the_brand(sm):
    uid = _seed_user(sm, brand_name="Crumb")
    assert _name_of(sm, _ensure(sm, uid)) == "Crumb"


def test_a_user_with_no_brand_name_still_gets_a_named_profile(sm):
    uid = _seed_user(sm)
    assert _name_of(sm, _ensure(sm, uid)) == "Personal"


def test_the_profile_becomes_active(sm):
    uid = _seed_user(sm)
    pid = _ensure(sm, uid)

    async def _check():
        async with sm() as db:
            assert (await db.get(User, uid)).active_account_id == pid
    _run(_check)


def test_existing_posts_move_onto_the_profile(sm):
    """The one that matters. list_posts filters on the active account, so a post
    left holding NULL is a post its owner can no longer see. Drop the adoption
    and every user's history disappears the moment they get a profile."""
    uid = _seed_user(sm)
    _seed_post(sm, uid, None, "old-one")
    _seed_post(sm, uid, None, "old-two")
    pid = _ensure(sm, uid)
    assert _topics_on(sm, pid) == {"old-one", "old-two"}


def test_a_post_already_tagged_with_a_client_brand_is_left_alone(sm):
    """Mutation guard: an agency user seeded now must not have their client
    work swept into their personal profile."""
    uid = _seed_user(sm)
    client = str(uuid.uuid4())

    async def _s():
        async with sm() as db:
            db.add(ManagedAccount(id=client, owner_user_id=uid, name="Client A"))
            await db.commit()
    _run(_s)
    _seed_post(sm, uid, client, "client-work")
    _seed_post(sm, uid, None, "own-work")
    pid = _ensure(sm, uid)

    assert _topics_on(sm, client) == {"client-work"}
    assert _topics_on(sm, pid) == {"own-work"}


def test_a_post_pointing_at_a_deleted_brand_is_adopted(sm):
    """posts.managed_account_id has no foreign key in the database — the model
    declares one but migration c7d8e9fa0b1c never created the constraint, so
    ondelete=SET NULL has never fired anywhere. Posts whose brand was deleted
    hold a dangling id, and since the filter is `== active` they are already
    invisible forever. Sweeping them here is the only chance to get them back."""
    uid = _seed_user(sm)
    _seed_post(sm, uid, "brand-that-was-deleted", "orphan")
    pid = _ensure(sm, uid)
    assert _topics_on(sm, pid) == {"orphan"}


def test_another_users_posts_are_never_touched(sm):
    uid, other = _seed_user(sm), _seed_user(sm)
    _seed_post(sm, other, None, "theirs")
    pid = _ensure(sm, uid)
    assert _topics_on(sm, pid) == set()


# ------------------------------------------------------------------ idempotence

def test_calling_it_twice_returns_the_same_profile(sm):
    uid = _seed_user(sm)
    assert _ensure(sm, uid) == _ensure(sm, uid)

    async def _check():
        async with sm() as db:
            rows = (await db.execute(select(ManagedAccount).where(
                ManagedAccount.owner_user_id == uid))).scalars().all()
            assert len(rows) == 1
    _run(_check)


def test_it_does_not_reseed_over_edited_fields(sm):
    """Mutation guard: re-seeding on every call would overwrite the profile with
    the stale User columns on every request once dual-write is in place."""
    uid = _seed_user(sm, niche="from-user")
    pid = _ensure(sm, uid)

    async def _edit():
        async with sm() as db:
            p = await db.get(ManagedAccount, pid)
            p.niche = "edited-in-the-profile"
            await db.commit()
    _run(_edit)
    _ensure(sm, uid)

    async def _check():
        async with sm() as db:
            assert (await db.get(ManagedAccount, pid)).niche == "edited-in-the-profile"
    _run(_check)


def test_an_agency_user_keeps_their_client_brands(sm):
    uid = _seed_user(sm)

    async def _s():
        async with sm() as db:
            db.add(ManagedAccount(id=str(uuid.uuid4()), owner_user_id=uid, name="A"))
            db.add(ManagedAccount(id=str(uuid.uuid4()), owner_user_id=uid, name="B"))
            await db.commit()
    _run(_s)
    _ensure(sm, uid)

    async def _check():
        async with sm() as db:
            rows = (await db.execute(select(ManagedAccount).where(
                ManagedAccount.owner_user_id == uid))).scalars().all()
            assert len(rows) == 3
            assert sum(1 for r in rows if r.is_primary) == 1
    _run(_check)


def test_the_database_refuses_a_second_primary(sm):
    """ensure_primary_profile is a select-then-insert, and two coroutines can
    interleave on the await between them even inside one worker. The partial
    unique index turns that race from silent data corruption into an error."""
    uid = _seed_user(sm)
    _ensure(sm, uid)

    async def _second():
        async with sm() as db:
            db.add(ManagedAccount(id=str(uuid.uuid4()), owner_user_id=uid,
                                  name="Sneaky", is_primary=True))
            await db.commit()
    with pytest.raises(IntegrityError):
        _run(_second)


def test_a_second_non_primary_brand_is_still_allowed(sm):
    """The index is partial — it must not turn into "one brand per user"."""
    uid = _seed_user(sm)
    _ensure(sm, uid)

    async def _second():
        async with sm() as db:
            db.add(ManagedAccount(id=str(uuid.uuid4()), owner_user_id=uid,
                                  name="Client A"))
            await db.commit()
    _run(_second)


# ------------------------------------------------------------------ the mirror

def test_the_mirror_copies_the_profile_back_onto_the_user(sm):
    """User's brand columns become a write-only rollback snapshot of the primary
    profile. Nothing reads them after this phase; a downgrade does."""
    user = User(id="u1", email="u1@ex.com")
    profile = ManagedAccount(id="p1", owner_user_id="u1", name="Crumb",
                             is_primary=True, **BRAND)
    mirror_primary_to_user(profile, user)
    for field, value in BRAND.items():
        assert getattr(user, field) == value, field


def test_the_mirror_touches_nothing_but_the_brand(sm):
    """Mutation guard: `name` exists on the profile and not on User, and copying
    a wider set would drag profile-only fields onto the user row."""
    user = User(id="u1", email="u1@ex.com", account_type="agency")
    mirror_primary_to_user(
        ManagedAccount(id="p1", owner_user_id="u1", name="Crumb", **BRAND), user)
    assert user.email == "u1@ex.com"
    assert user.account_type == "agency"
    assert not hasattr(user, "is_primary") or user.is_primary is None
