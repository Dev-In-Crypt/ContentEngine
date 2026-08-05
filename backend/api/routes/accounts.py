"""Brand profiles: CRUD + active-profile switch + per-profile logo.

Every route is owner-scoped (owner_user_id == user.id, 404 on a miss) and edits a
brand's IDENTITY only. Security stays on user_id — the active profile only scopes
the composer view and supplies the brand generation uses.

Since UX phase 2 every user owns one profile marked is_primary, which they cannot
delete and which /api/settings/* edits. An agency has more rows beside it.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db
from models.database import ManagedAccount as ManagedAccountModel
from models.database import User as UserModel
from models.schemas import (
    AccountCreate, AccountListResponse, AccountOut, AccountSwitch, AccountUpdate,
)
from services import logo_store
from services.managed_account import ensure_primary_profile, owned_account

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

_LOGO_TYPES = {"image/png", "image/webp", "image/jpeg"}
_LOGO_MAX_BYTES = 20 * 1024 * 1024


def _logo_key(account_id: str) -> str:
    return f"acct_{account_id}"


def _account_out(a: ManagedAccountModel) -> AccountOut:
    return AccountOut(
        id=a.id, name=a.name, brand_voice_preset=a.brand_voice_preset,
        brand_voice_custom=a.brand_voice_custom, niche=a.niche,
        target_audience=a.target_audience, brand_name=a.brand_name,
        slide_accent_color=a.slide_accent_color, slide_text_box_color=a.slide_text_box_color,
        has_logo=bool(logo_store.path_for(_logo_key(a.id))))


@router.get("", response_model=AccountListResponse)
async def list_accounts(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> AccountListResponse:
    rows = (await db.execute(
        select(ManagedAccountModel).where(ManagedAccountModel.owner_user_id == user.id)
        # Primary first, so the SPA can render the list in order without knowing
        # which row that is. created_at alone would not do it: an existing
        # agency's client brands predate the profile seeded for them.
        .order_by(ManagedAccountModel.is_primary.desc(),
                  ManagedAccountModel.created_at.asc())
    )).scalars().all()
    return AccountListResponse(
        accounts=[{"id": a.id, "name": a.name, "is_primary": bool(a.is_primary)}
                  for a in rows],
        active_account_id=user.active_account_id)


@router.post("", response_model=AccountOut)
async def create_account(
    body: AccountCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> AccountOut:
    acct = ManagedAccountModel(owner_user_id=user.id, name=body.name.strip())
    db.add(acct)
    await db.commit()
    await db.refresh(acct)
    return _account_out(acct)


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> AccountOut:
    return _account_out(await owned_account(db, account_id, user))


@router.put("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: str,
    body: AccountUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> AccountOut:
    acct = await owned_account(db, account_id, user)
    for field in ("name", "brand_voice_preset", "brand_voice_custom", "niche",
                  "target_audience", "brand_name", "slide_accent_color", "slide_text_box_color"):
        value = getattr(body, field)
        if value is not None:                       # "" clears; None leaves unchanged
            setattr(acct, field, value.strip() or None if isinstance(value, str) else value)
    await db.commit()
    return _account_out(acct)


@router.delete("/{account_id}", response_model=dict)
async def delete_account(
    account_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    acct = await owned_account(db, account_id, user)
    if acct.is_primary:
        raise HTTPException(
            status_code=409, detail="Your main profile can't be deleted.")

    primary = await ensure_primary_profile(db, user)
    # Move the work, don't untag it. NULL used to mean "Personal" and was
    # visible; since UX phase 2 it is visible to nobody, so leaving posts
    # behind here would silently destroy everything made under this brand.
    # Explicit because the database wouldn't do it anyway: posts has no foreign
    # key to managed_accounts, so the model's ondelete="SET NULL" is fiction.
    await db.execute(text(
        "UPDATE posts SET managed_account_id = :primary "
        "WHERE managed_account_id = :deleted"),
        {"primary": primary.id, "deleted": acct.id})

    logo_store.delete(_logo_key(acct.id))
    if user.active_account_id == acct.id:
        user.active_account_id = primary.id
    await db.delete(acct)
    await db.commit()
    return {"status": "deleted"}


@router.post("/switch", response_model=dict)
async def switch_account(
    body: AccountSwitch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    if body.account_id is None:
        # null used to mean "Personal", which no longer exists as an absence. A
        # browser on a cached index.html keeps sending it, so it is read as "my
        # own profile" — what the person meant by Personal anyway. A 422 here
        # would turn a stale cache into a broken switcher.
        acct = await ensure_primary_profile(db, user)
    else:
        acct = await owned_account(db, body.account_id, user)   # 404 if not owned
    user.active_account_id = acct.id
    await db.commit()
    return {"active_account_id": user.active_account_id}


@router.post("/{account_id}/logo", response_model=dict)
async def put_account_logo(
    account_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    file: UploadFile = File(...),
) -> dict:
    acct = await owned_account(db, account_id, user)
    if file.content_type not in _LOGO_TYPES:
        raise HTTPException(status_code=415, detail="Allowed: png, webp, jpeg.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")
    path = logo_store.save(_logo_key(acct.id), data, file.content_type)
    acct.logo_path = str(path)
    await db.commit()
    return {"has_logo": True}


@router.delete("/{account_id}/logo", response_model=dict)
async def delete_account_logo(
    account_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    acct = await owned_account(db, account_id, user)
    logo_store.delete(_logo_key(acct.id))
    acct.logo_path = None
    await db.commit()
    return {"has_logo": False}


@router.get("/{account_id}/logo/image")
async def get_account_logo_image(
    account_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> FileResponse:
    await owned_account(db, account_id, user)
    path = logo_store.path_for(_logo_key(account_id))
    if not path:
        raise HTTPException(status_code=404, detail="No logo set")
    return FileResponse(path)
