"""Registration, login, email verification, password reset.

Local (desktop) mode never uses these — get_current_user returns the implicit
local owner. Cloud mode: register/login → JWT; verify/reset via emailed tokens.
"""
import logging
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from api.deps import get_current_user, get_db
from api.ratelimit import limiter
from models.database import User as UserModel
from services.auth import (
    create_access_token, create_purpose_token, decode_purpose_token,
    hash_password, verify_password,
)
from services.email import send_reset_email, send_verify_email
from services.managed_account import ensure_primary_profile
from services.gdpr import UPLOADS_ROOT

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_VERIFY_TTL = timedelta(hours=24)
_RESET_TTL = timedelta(hours=1)


#: "creator" — one channel of your own. "business" — the sources → leads →
#: approval product. "agency" — several client brands, which is the creator
#: engine over many profiles, so it is deliberately NOT let through
#: require_business. The SPA still gates on == "business", which lands an agency
#: in the creator shell; its own navigation arrives with UX phase 3.
_ACCOUNT_TYPES = {"creator", "business", "agency"}


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    # Which product the sign-up came from (the landing tab). Unknown/garbage
    # values fall back to "creator" rather than 422 — a stray query-param must
    # never block registration.
    account_type: str = "creator"

    @field_validator("account_type", mode="before")
    @classmethod
    def _normalise_account_type(cls, v: object) -> str:
        s = str(v or "").strip().lower()
        return s if s in _ACCOUNT_TYPES else "creator"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: str
    email: str
    is_local: bool = False
    is_admin: bool = False
    email_verified: bool = False
    account_type: str = "creator"
    active_account_id: Optional[str] = None


class ForgotRequest(BaseModel):
    email: EmailStr


class ResetRequest(BaseModel):
    token: str
    password: str = Field(..., min_length=8, max_length=200)


@router.post("/register", response_model=TokenResponse)
@limiter.limit("5/minute;30/hour")
async def register(
    request: Request,
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    email = body.email.lower()
    existing = (await db.execute(
        select(UserModel).where(UserModel.email == email)
    )).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = UserModel(email=email, password_hash=hash_password(body.password),
                     account_type=body.account_type)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # Every user owns a brand profile from their first second (UX phase 2). Done
    # here rather than left to the lazy repair in get_current_user so the row
    # exists before anything can read it.
    await ensure_primary_profile(db, user)
    await send_verify_email(email, create_purpose_token(user.id, "verify", _VERIFY_TTL))
    return TokenResponse(access_token=create_access_token(user.id, user.token_version))


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute;50/hour")
async def login(
    request: Request,
    body: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    user = (await db.execute(
        select(UserModel).where(UserModel.email == body.email.lower())
    )).scalars().first()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash or ""):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return TokenResponse(access_token=create_access_token(user.id, user.token_version))


@router.get("/verify")
async def verify_email(token: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    user_id = decode_purpose_token(token, "verify")
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    user = await db.get(UserModel, user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    user.email_verified = True
    await db.commit()
    return {"status": "verified"}


@router.post("/resend-verification")
@limiter.limit("3/minute;10/hour")
async def resend_verification(
    request: Request,
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    if not user.email_verified:
        await send_verify_email(user.email, create_purpose_token(user.id, "verify", _VERIFY_TTL))
    return {"status": "ok"}


@router.post("/forgot")
@limiter.limit("3/minute;10/hour")
async def forgot_password(
    request: Request,
    body: ForgotRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    user = (await db.execute(
        select(UserModel).where(UserModel.email == body.email.lower())
    )).scalars().first()
    if user and user.password_hash:   # local user has no password → skip
        await send_reset_email(user.email, create_purpose_token(user.id, "reset", _RESET_TTL))
    # Always 200 — don't reveal whether an email is registered.
    return {"status": "ok"}


@router.post("/reset")
@limiter.limit("5/minute;20/hour")
async def reset_password(
    request: Request,
    body: ResetRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    user_id = decode_purpose_token(body.token, "reset")
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    user = await db.get(UserModel, user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    user.password_hash = hash_password(body.password)
    user.email_verified = True   # resetting via emailed link proves email ownership
    user.token_version = (user.token_version or 0) + 1   # revoke all existing sessions
    await db.commit()
    return {"status": "ok"}


@router.post("/logout-all")
async def logout_all(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Invalidate every existing session for this user (e.g. a lost/stolen token)
    by bumping token_version. The caller's current token stops working too."""
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    return {"status": "ok"}


def _me_payload(user: UserModel) -> MeResponse:
    return MeResponse(
        id=user.id, email=user.email,
        is_local=bool(user.is_local), is_admin=bool(user.is_admin),
        email_verified=bool(user.email_verified),
        account_type=user.account_type or "creator",
        active_account_id=getattr(user, "active_account_id", None),
    )


@router.get("/me", response_model=MeResponse)
async def me(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> MeResponse:
    return _me_payload(user)


class AccountTypeRequest(BaseModel):
    account_type: str


@router.put("/account-type", response_model=MeResponse)
async def set_account_type(
    body: AccountTypeRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeResponse:
    """Switch the signed-in account between the Creators and Business products.
    Both run on one engine; account_type only decides which experience shows.
    One email = one login, so an in-app toggle is the only way to move between
    products (you can't register the same email twice). Unlike the sign-up
    normaliser, an unknown value here is a 422 — an explicit switch shouldn't
    silently land somewhere the caller didn't ask for."""
    t = str(body.account_type or "").strip().lower()
    if t not in _ACCOUNT_TYPES:
        raise HTTPException(status_code=422, detail="Unknown account_type")
    user.account_type = t
    await db.commit()
    await db.refresh(user)
    return _me_payload(user)


# ===== GDPR: take your data with you, or take it off our servers =====

class DeleteAccountRequest(BaseModel):
    # The current password, re-entered. See the endpoint docstring.
    password: str = Field(..., min_length=1, max_length=200)


@router.get("/export")
async def export_account(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse:
    """Download everything we hold about this account as a ZIP.

    Built to a temp file rather than memory: a busy account's slides and reels
    run to hundreds of megabytes, and buffering that per request is how one
    export takes the process down. The file is deleted after the response is
    sent, whether or not the client finished reading it.
    """
    from services import gdpr

    data = await gdpr.collect_user_data(db, user)
    raw_paths = await gdpr.user_media_paths(db, user)
    files = gdpr.safe_media_files(raw_paths, UPLOADS_ROOT)

    tmp = Path(tempfile.mkdtemp(prefix="ce-export-")) / "content-engine-export.zip"
    gdpr.write_export_zip(tmp, data, files, UPLOADS_ROOT)
    stamp = datetime.now().strftime("%Y%m%d")
    return FileResponse(
        tmp, media_type="application/zip",
        filename=f"content-engine-export-{stamp}.zip",
        background=BackgroundTask(shutil.rmtree, tmp.parent, ignore_errors=True),
    )


@router.post("/delete")
@limiter.limit("5/hour")
async def delete_account(
    request: Request,
    body: DeleteAccountRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Erase the account and everything attached to it. Irreversible.

    The current password is required even though the request is already
    authenticated: a token lifted from a shared machine should not be able to
    destroy someone's work. Local (desktop) mode has no account to erase — the
    data is on the user's own disk.
    """
    from services import gdpr

    if getattr(user, "is_local", False):
        raise HTTPException(
            status_code=400,
            detail="Local mode has no cloud account — delete the app's data folder.")
    if not verify_password(body.password, user.password_hash or ""):
        raise HTTPException(status_code=403, detail="That password is not correct.")

    email = user.email
    counts = await gdpr.delete_user_data(db, user, root=UPLOADS_ROOT)
    await db.commit()
    log.info("Account erased: %s (%s)", email, counts)
    return {"status": "deleted", "deleted": counts}
