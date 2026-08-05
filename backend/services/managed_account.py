"""Brand profiles: resolution, seeding, and the ownership gate.

A profile holds one brand's identity. It is NEVER a security boundary — posts are
always owned by user_id; the active profile only scopes the view and supplies the
brand identity for generation. Its columns mirror User's brand fields, so the
resolvers (resolve_user_profile / resolve_user_brand_voice /
apply_brand_slide_style) accept it directly via duck typing.

Since UX phase 2 every user owns exactly one profile with `is_primary` set, seeded
from the brand columns that used to live on User. An agency has more rows next to
it. "Personal", which used to mean no row at all, no longer exists.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ManagedAccount as ManagedAccountModel
from models.database import User as UserModel

#: The brand identity that lives on both shapes. The single list every copy in
#: either direction iterates — seeding, and the mirror back onto User.
BRAND_FIELDS = (
    "brand_voice_preset", "brand_voice_custom", "niche", "target_audience",
    "brand_name", "slide_accent_color", "slide_text_box_color", "logo_path",
)


async def resolve_active_account(db: AsyncSession,
                                 user: UserModel) -> ManagedAccountModel:
    """The brand profile this user is working in. Never None.

    A pointer at a deleted or foreign brand used to resolve to None, which meant
    "Personal" and was a working state. Since UX phase 2 it isn't: the caller
    would render with no brand and list no posts. So an unresolvable pointer
    falls back to the primary and is repaired on the way, rather than being
    re-resolved on every subsequent request.

    The fallback never widens ownership — an id belonging to someone else lands
    on the caller's own profile, not on theirs.
    """
    account_id = getattr(user, "active_account_id", None)
    if account_id:
        acct = await db.get(ManagedAccountModel, account_id)
        if acct is not None and acct.owner_user_id == user.id:
            return acct
    profile = await ensure_primary_profile(db, user)
    # Repaired here rather than inside ensure_primary_profile, which must stay
    # free to hand back the primary — for the User-columns mirror in 2.5 —
    # without moving whichever brand the user is actually working in.
    if user.active_account_id != profile.id:
        user.active_account_id = profile.id
        await db.commit()
    return profile


async def brand_for_post(db: AsyncSession, post,
                         user: UserModel) -> ManagedAccountModel:
    """The brand a post was made under, for re-rendering its slides.

    Deliberately the post's brand and not the active one. Someone running
    several clients who re-renders a slide of a post made for Client A must get
    A's colours and logo even after switching to Client B — resolving the active
    brand here would be a fresh instance of exactly the bug UX phase 2 set out
    to fix, the composer showing one brand and the editor another.

    Falls back to the user's primary for a post from before profiles existed, or
    one whose brand has been deleted. A tag naming someone else's brand is not
    honoured: ownership lives on user_id and this must not become a way around it.
    """
    account_id = getattr(post, "managed_account_id", None)
    if account_id:
        acct = await db.get(ManagedAccountModel, account_id)
        if acct is not None and acct.owner_user_id == user.id:
            return acct
    return await ensure_primary_profile(db, user)


async def primary_profile(db: AsyncSession, user: UserModel):
    """The user's primary profile, or None if they have not been seeded yet."""
    return (await db.execute(
        select(ManagedAccountModel).where(
            ManagedAccountModel.owner_user_id == user.id,
            ManagedAccountModel.is_primary.is_(True))
    )).scalar_one_or_none()


async def ensure_primary_profile(db: AsyncSession,
                                 user: UserModel) -> ManagedAccountModel:
    """The user's primary profile, seeding it on first sight.

    Three writes, and the data migration performs exactly these three in SQL for
    everyone at once. Keeping them identical is the point: a user seeded here and
    a user seeded by the migration must be indistinguishable afterwards.

    1. the profile, copied from the user's own brand columns
    2. active_account_id, pointed at it
    3. every post of theirs that carries no live profile, adopted onto it

    Step 3 is not housekeeping. `list_posts` filters
    `managed_account_id == active_account_id`, so a post left holding NULL is one
    its owner can no longer see.
    """
    existing = await primary_profile(db, user)
    if existing is not None:
        # Never re-seed: after dual-write lands, User's columns are a stale
        # snapshot and copying them again would undo real edits.
        if user.active_account_id != existing.id and not user.active_account_id:
            user.active_account_id = existing.id
            await db.commit()
        return existing

    profile = ManagedAccountModel(
        id=str(uuid.uuid4()), owner_user_id=user.id, is_primary=True,
        name=(user.brand_name or "").strip() or "Personal",
        created_at=user.created_at,
        **{f: getattr(user, f, None) for f in BRAND_FIELDS},
    )
    db.add(profile)
    await db.flush()

    # NOT IN, not "IS NULL": posts pointing at an already-deleted brand are
    # adopted too. posts.managed_account_id has no foreign key in the database
    # (migration c7d8e9fa0b1c created the column and index but no constraint),
    # so ondelete=SET NULL has never fired and those rows are stranded.
    await db.execute(text(
        "UPDATE posts SET managed_account_id = :pid WHERE user_id = :uid AND ("
        "  managed_account_id IS NULL"
        "  OR managed_account_id NOT IN (SELECT id FROM managed_accounts))"
    ), {"pid": profile.id, "uid": user.id})

    user.active_account_id = profile.id
    await db.commit()
    return profile


def mirror_primary_to_user(profile, user: UserModel) -> None:
    """Copy the primary profile's brand back onto User's legacy columns.

    Those columns are a write-only rollback snapshot: nothing reads them after
    UX phase 2, and a downgrade restores from them. This is their only writer —
    if a second one appears, the snapshot stops being trustworthy.
    """
    for field in BRAND_FIELDS:
        setattr(user, field, getattr(profile, field, None))


async def owned_account(db: AsyncSession, account_id: str, user: UserModel) -> ManagedAccountModel:
    """Fetch a managed account this user owns, else 404 (don't reveal another's)."""
    acct = (await db.execute(
        select(ManagedAccountModel).where(
            ManagedAccountModel.id == account_id,
            ManagedAccountModel.owner_user_id == user.id)
    )).scalar_one_or_none()
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return acct
