"""How many posts an account gets on OUR key before it needs its own.

The product is bring-your-own-keys: people pay the model vendor directly and we
never hold the bill. Onboarding is the one exception — a brand-new account has
no key yet, and asking for one before showing anything is exactly what the UX
document says not to do. So the first post is written on the application's key,
and this module is the only thing allowed to say how many of those there are.

**The allowance is spent before the model is called, and committed there.**
That ordering is the whole design. A counter incremented after a successful
generation never rises when the process dies mid-call, when two requests arrive
together, or when the provider answers slowly enough that somebody presses the
button again — and each of those ends with us paying twice for one free post.
Refunding a reservation that demonstrably bought nothing is the cheap direction
to be wrong in; the expensive direction is an unlimited generator on our key.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User

log = logging.getLogger(__name__)

#: One. Enough to end onboarding on something real, few enough that the worst an
#: attacker gets per account is a single short completion about a niche they
#: typed in themselves. UX phase 6 raises this and adds the anonymous half.
FREE_POST_LIMIT = 1


def remaining(user: User) -> int:
    """How many free posts are left, never negative.

    Clamped because the limit can be lowered in a later release while accounts
    already sit above it, and "you have -2 posts left" is not a sentence to show
    anybody.
    """
    used = user.free_generations_used or 0
    return max(0, FREE_POST_LIMIT - used)


async def reserve(db: AsyncSession, user: User) -> bool:
    """Claim one free post, or refuse. Commits before returning.

    The commit is not tidiness: the caller's next move is a slow network call to
    a paid provider, and an increment still pending on a session that a failure
    would roll back is an allowance handed straight back.
    """
    if remaining(user) <= 0:
        return False
    user.free_generations_used = (user.free_generations_used or 0) + 1
    await db.commit()
    return True


async def refund(db: AsyncSession, user: User) -> None:
    """Give the allowance back, for the one case that deserves it: the model was
    called and returned nothing usable, so the account paid for silence.

    Clamped at zero. A double refund — a retry, a stray `except` — must not mint
    allowance out of nothing, which is the failure this whole module exists to
    prevent.
    """
    used = user.free_generations_used or 0
    if used <= 0:
        return
    user.free_generations_used = used - 1
    await db.commit()
