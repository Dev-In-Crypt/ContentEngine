"""What the product has already shown this person.

UX phase 8 makes each feature appear at the moment it starts to mean something —
brand rules after somebody first rewrites the AI, a second profile at the moment
they publish into the wrong one. Every one of those moments needs the same thing
underneath: a fact that survives a reload, a new browser and another device, and
that can only ever be set once.

Two rules carry the design, and both are about what does NOT belong here.

**Only what was shown, never what was counted.** How many posts somebody has
made is a question the posts table already answers. Copying it into a milestone
would create a second number, and two numbers about one fact eventually
disagree — usually in the direction where a feature appears for somebody who has
not earned it, or fails to for somebody who has. What lives here is what the
data cannot reconstruct: that a hint was displayed, that it was waved away, that
an edit happened at a moment nobody kept a record of.

**Never un-recorded.** A feature that appeared and then vanished because a count
dipped below its threshold is worse than one that was always there: it makes
people doubt what they saw and go looking for a setting that never existed. So
`record` is idempotent and there is deliberately no `forget`.

The names are a closed set. A typo in a milestone name is a feature that never
appears and a test that never fails, which is the quietest possible bug.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User as UserModel

#: Somebody rewrote what the AI wrote. The moment brand rules start to mean
#: something: they have an opinion and have just expressed it.
EDITED_AI_TEXT = "edited_ai_text"

#: They were asked whether to remember that edit as a rule, and said no.
RULES_HINT_DISMISSED = "rules_hint_dismissed"

#: They were offered sources — "would you like topics to find themselves?"
SOURCES_OFFERED = "sources_offered"

#: They have been told, once, that publishing connections belong to the account
#: and are shared by every brand profile.
#:
#: This was SECOND_PROFILE_OFFERED, on the assumption that a network could be
#: connected per profile and a second profile was therefore the answer to
#: "publishing to an account this profile has no keys for". `UserCredentials` is
#: keyed on the user and `ManagedAccount` holds no connection at all, so that
#: answer would have been an offer the product cannot honour. Renamed rather
#: than kept: a milestone whose name describes something else is a lie waiting
#: to be read as a promise. Never written under the old name, so nothing to
#: migrate.
CONNECTIONS_ARE_SHARED = "connections_are_shared"

#: The journal appeared, after the tenth published post.
JOURNAL_UNLOCKED = "journal_unlocked"

#: The team screen appeared, on the second profile or the first invitation.
TEAM_UNLOCKED = "team_unlocked"

ALL: tuple[str, ...] = (
    EDITED_AI_TEXT,
    RULES_HINT_DISMISSED,
    SOURCES_OFFERED,
    CONNECTIONS_ARE_SHARED,
    JOURNAL_UNLOCKED,
    TEAM_UNLOCKED,
)


def all_for(user: UserModel) -> dict:
    """Everything this account has reached, as {name: when}.

    NULL reads as empty, which is every account that existed before the column
    did — the only reading that does not crash on them.
    """
    return dict(user.milestones or {})


def reached(user: UserModel, name: str) -> bool:
    return name in all_for(user)


async def record(db: AsyncSession, user: UserModel, name: str) -> None:
    """Mark a milestone as reached, once. Commits.

    Committing is not tidiness: the caller's next move is to render a screen
    that depends on this, and a milestone still pending on a session that a
    later failure rolls back is a hint shown to the same person twice.
    """
    if name not in ALL:
        raise ValueError(f"Unknown milestone: {name!r}")
    current = all_for(user)
    if name in current:
        return                      # reached once; the first time is the fact
    current[name] = datetime.now(timezone.utc).isoformat()
    user.milestones = current
    # The column is JSON and we replaced the dict wholesale, but a caller that
    # mutates in place would otherwise leave SQLAlchemy unaware there is
    # anything to write — cheap insurance against the next person's shortcut.
    flag_modified(user, "milestones")
    await db.commit()


async def record_all(db: AsyncSession, user: UserModel) -> None:
    """Reveal everything at once — the escape hatch behind "Show all features".

    Without it, the first person who saw a feature on a colleague's screen and
    cannot find it goes to support rather than to the product. Reaching one that
    was already reached leaves its original timestamp alone: this reveals, it
    does not rewrite history.
    """
    current = all_for(user)
    now = datetime.now(timezone.utc).isoformat()
    for name in ALL:
        current.setdefault(name, now)
    user.milestones = current
    flag_modified(user, "milestones")
    await db.commit()
