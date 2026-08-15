"""Bad-news pause (Phase 6): negative events are flagged sensitive so the user is
warned before posting. We fix behaviour, not the detector's accuracy (doc §9)."""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.source_poller as poller
from models.database import Base, Lead, Source, Workspace
from services.event_selector import detect_bad_news
from services.sources.base import FetchedItem


def _item(title, body=""):
    return FetchedItem(external_id="x", kind="rss", title=title, url="u",
                       published_at=None, body=body)


@pytest.mark.parametrize("title", [
    "We had a major outage today",
    "Security breach disclosed (CVE-2026-1)",
    "Announcing layoffs across the company",
    "Price increase coming next month",
    "We're sorry for the downtime",
    "Service disruption affecting logins",
])
def test_bad_news_detected(title):
    assert detect_bad_news(_item(title)) is True


@pytest.mark.parametrize("title", [
    "Launched v2 with new features",
    "New pricing: a cheaper Pro plan",
    "We hit 1 million users",
    "Introducing dark mode",
])
def test_good_news_not_flagged(title):
    assert detect_bad_news(_item(title)) is False


# ── the warning has to stay rare enough to be read ──────────────────────────

# A real one. go-ethereum v1.17.5: 5275 characters, an ordinary maintenance
# release, and the whole of the old word list matched exactly twice — "failing"
# and "shutdown", both past character 2000, both inside the list of merged
# pull requests. Shortened here to the same shape.
_MAINTENANCE_RELEASE = (
    "This is a maintenance release with accumulated bug fixes and "
    "improvements, and is recommended for all users. It continues the "
    "implementation work for the upcoming Amsterdam hardfork.\n\n"
    + "* core: assorted improvements to the transaction pool\n" * 40
    + "* eth: fix a failing test\n"
    + "* node: close the database cleanly on shutdown\n"
)


def test_a_changelog_is_not_bad_news_because_page_three_says_failing():
    """Every lead the product produced on a live account came back flagged, so
    the warning meant nothing and the dialog in front of it was something to
    click past. The words were "failing" and "shutdown", two thousand characters
    down a list of merged pull requests — ordinary changelog vocabulary, not
    news about the company."""
    assert detect_bad_news(
        _item("Grav-Torque Pad (v1.17.5)", body=_MAINTENANCE_RELEASE)) is False


@pytest.mark.parametrize("title", [
    "Postponing the launch to next quarter",
    "Deprecating the v1 API",
    "Our database was exposed for six hours",
])
def test_the_same_words_still_warn_when_they_are_the_headline(title):
    """The narrowing is about WHERE a word sits, not which words count. Said up
    front, every one of these is the news."""
    assert detect_bad_news(_item(title, body=_MAINTENANCE_RELEASE)) is True


def test_a_crisis_word_still_reaches_the_bottom_of_the_page():
    """Words that cannot mean anything else keep the whole body. An incident
    report that buries "we were breached" on page two is exactly the case the
    warning exists for."""
    body = "Routine notes.\n" + ("filler line\n" * 300) + "We were breached.\n"
    assert detect_bad_news(_item("Service update", body=body)) is True


def test_a_hackathon_is_not_a_hack():
    assert detect_bad_news(_item("Join our hackathon this weekend")) is False


class _FakeFetcher:
    def __init__(self, items):
        self._items = items

    async def fetch(self, url, since=None):
        return self._items


def test_poller_sets_sensitive_flag(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bn.db'}")

    async def _run():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SM = async_sessionmaker(eng, expire_on_commit=False)
        async with SM() as db:
            ws = Workspace(owner_user_id="u", name="w")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)
            src = Source(workspace_id=ws.id, url="u", kind="rss", status="ok", active=True)
            db.add(src)
            await db.commit()
            await db.refresh(src)
        items = [
            FetchedItem(external_id="1", kind="rss", title="Major outage and data breach",
                        url="u", published_at=None, body="Systems were down."),
            FetchedItem(external_id="2", kind="rss", title="Launched our new dashboard",
                        url="u", published_at=None, body="A shiny new feature."),
        ]
        monkeypatch.setattr(poller, "get_source_fetcher",
                            lambda kind, ssl_verify=True: _FakeFetcher(items))
        await poller.poll_all(SM)
        async with SM() as db:
            rows = {r.what_happened: r.sensitive
                    for r in (await db.execute(select(Lead))).scalars().all()}
        await eng.dispose()
        return rows

    rows = asyncio.run(_run())
    assert rows["Major outage and data breach"] is True    # mutation guard: flag must be set
    assert rows["Launched our new dashboard"] is False
