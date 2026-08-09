"""Team invitations — the agency screen that opens no doors yet.

Four routes and one table. Accepting an invitation records that somebody is on
a team and grants them nothing: shared access to brands, posts and keys is a
later phase, and both the email and the screen say so in plain words. Shipping
the record first is deliberate — it is the part that has to exist before access
can be built on top of it, and it is safe precisely because it is inert.

Where the risk actually is:

  * `require_agency` on every route, including the read. An invitation list is
    a list of colleagues' email addresses.
  * The accept route checks the ADDRESS, not just the token. A token is a
    bearer string in an email, and email gets forwarded.
  * The pending-uniqueness index does the work rather than a select-then-insert
    check, so a double-submit is a 409 instead of two live invitations.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db, require_agency
from models.database import TeamInvitation
from models.database import User as UserModel
from models.schemas import TeamInvitationOut, TeamInviteAccept, TeamInviteRequest
from services import milestones
from services.auth import create_purpose_token, decode_purpose_token
from services.email import send_team_invite_email
from api.ratelimit import limiter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/team", tags=["team"])

#: Long enough to survive a holiday, short enough that a leaked mailbox from
#: last year is not a live credential.
INVITE_TTL = timedelta(days=14)
_PURPOSE = "team_invite"


def _fold(email: str) -> str:
    """One address, one row. "A@x" and "a@x" are the same mailbox, and storing
    both would slip straight past the pending-uniqueness index."""
    return (email or "").strip().lower()


@router.get("/invitations", response_model=list[TeamInvitationOut],
            dependencies=[Depends(require_agency)])
async def list_invitations(
    db: AsyncSession = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    rows = (await db.execute(
        select(TeamInvitation)
        .where(TeamInvitation.owner_user_id == user.id)
        .order_by(TeamInvitation.created_at.desc())
    )).scalars().all()
    return [TeamInvitationOut.model_validate(r) for r in rows]


@router.post("/invitations", response_model=TeamInvitationOut,
             dependencies=[Depends(require_agency)])
@limiter.limit("10/minute;50/hour")
async def invite(
    request: Request,
    body: TeamInviteRequest,
    db: AsyncSession = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    email = _fold(body.email)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if email == _fold(user.email or ""):
        raise HTTPException(status_code=400, detail="That's your own address.")

    row = TeamInvitation(owner_user_id=user.id, email=email, status="pending")
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # The partial index, not a prior SELECT: two submits a millisecond apart
        # both pass a check-then-insert and only one of the two rows would ever
        # be revoked.
        await db.rollback()
        raise HTTPException(status_code=409,
                            detail="That address already has a pending invitation.")
    await db.refresh(row)
    # An agency that has invited somebody keeps the Team screen even if it drops
    # back to a single profile (UX phase 8.5). Recorded after the commit and
    # inside a try: the invitation is the fact, and it has already been sent to
    # the database — a milestone failure must not turn a live invitation into a
    # 500 the caller reads as "it did not happen".
    try:
        await milestones.record(db, user, milestones.TEAM_UNLOCKED)
    except Exception:
        log.exception("Could not record the team milestone for user=%s", user.id)

    token = create_purpose_token(row.id, _PURPOSE, INVITE_TTL)
    await send_team_invite_email(email, token, user.email or "")
    return TeamInvitationOut.model_validate(row)


@router.delete("/invitations/{invitation_id}",
               dependencies=[Depends(require_agency)])
async def revoke(
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    row = (await db.execute(
        select(TeamInvitation).where(TeamInvitation.id == invitation_id,
                                     TeamInvitation.owner_user_id == user.id)
    )).scalar_one_or_none()
    # 404 rather than 403: another agency's invitation is not ours to confirm
    # the existence of.
    if row is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    row.status = "revoked"
    await db.commit()
    return {"ok": True}


@router.post("/invitations/accept", response_model=TeamInvitationOut)
async def accept(
    body: TeamInviteAccept,
    db: AsyncSession = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """Accept an invitation addressed to you.

    Not behind require_agency: the person accepting is whoever was invited, and
    they are ordinarily a creator. The gate here is the address on the row.
    """
    invitation_id = decode_purpose_token(body.token, _PURPOSE)
    if not invitation_id:
        raise HTTPException(status_code=400, detail="That invitation link is not valid.")

    row = await db.get(TeamInvitation, invitation_id)
    if row is None:
        raise HTTPException(status_code=400, detail="That invitation link is not valid.")
    if row.status != "pending":
        raise HTTPException(status_code=400,
                            detail="That invitation is no longer open.")
    # The token proves the LINK is genuine, not that the right person is holding
    # it. Invitation emails get forwarded; without this, whoever reads the mail
    # joins the team.
    if _fold(user.email or "") != _fold(row.email):
        raise HTTPException(status_code=403,
                            detail="That invitation was sent to a different address.")

    row.status = "accepted"
    row.accepted_user_id = user.id
    row.accepted_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return TeamInvitationOut.model_validate(row)
