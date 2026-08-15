"""Deciding which source items are worth posting about — explainable rules only.

The riskiest part of the Business module is selection: too much junk and a person
stops trusting the feed. So the rules are deliberately simple and legible (doc §5),
never ML — every verdict comes with a one-line reason a human can sanity-check.

A verdict is one of:
- "worthy"    — a real event (customer impact, a concrete result, a launch, a change).
- "weak"      — nothing wrong, but no strong signal; still shown, just flagged.
- "duplicate" — a matching item was already seen recently (caller supplies the window).

Pure functions so the rules are testable and the selection is reproducible.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from services.sources.base import FetchedItem

# Customer-facing impact: price, availability, limits, security — the things a
# reader actually reacts to.
_IMPACT = re.compile(
    r"\b(?:price|pricing|cost|free|plan|tier|limit|quota|deprecat\w*|discontinu\w*|"
    r"sunset|end of life|eol|breaking|security|vulnerab\w*|outage|incident|downtime|"
    # "release" is NOT here, for the reason written next to _LAUNCH below: every
    # changelog entry literally says it, so it separates nothing. It survived here
    # until a calibration run over real repositories returned prisma 8 of 8 worthy,
    # each one on that single word — visible only once reasons quoted their match.
    r"availab\w*|launch\w*|now available|general availability|\bga\b|"
    r"acqui\w*|partnership|integration|support for|support[s]? )\b",
    re.IGNORECASE,
)
# A launch/ship signal specifically. Note: the bare word "release" is deliberately
# NOT here — every changelog entry literally says "release", so it's noise, not a
# launch signal (it inflated precision; see hypothesis test 2).
_LAUNCH = re.compile(
    r"\b(?:launch\w*|introduc\w*|announc\w*|ship\w*|now available|"
    r"unveil\w*|rolling out|roll[s]? out)\b", re.IGNORECASE)

# A dev-channel pre-release (nightly/alpha/canary/etc.) — a build a company ships
# continuously, not something it posts about. Milestone pre-releases (beta/rc/
# preview) are deliberately EXCLUDED: those get announced, so they stay worthy.
_DEV_PRERELEASE = re.compile(
    r"\b(?:alpha|canary|nightly|snapshot|dev)\b|-(?:alpha|canary|nightly|dev)\.?\d*",
    re.IGNORECASE)

# A semantic-version PATCH tag (x.y.Z with Z > 0) — the churn tier. On its own a
# patch is rarely post-worthy; it needs a strong signal (a number, a before→after
# change, a launch, or security) to clear the bar. The minor component forbids a
# leading zero (semver rule) so a zero-padded date like "2026.07.21" isn't mistaken
# for a patch and wrongly demoted.
_SEMVER_PATCH = re.compile(r"\bv?\d+\.(?:0|[1-9]\d*)\.([1-9]\d*)\b")
# Security is the one thing that keeps a patch worthy on its own.
_SECURITY = re.compile(r"\b(?:security|vulnerab\w*|\bcve\b)\b", re.IGNORECASE)
# A before→after change with a concrete result.
_CHANGE = re.compile(
    r"\b(?:up to|from\s+\S+\s+to\s+\S+|increas\w*|decreas\w*|reduc\w*|doubl\w*|"
    r"tripl\w*|faster|cheaper|now \d)\b", re.IGNORECASE)
# A quantified result: percentage, currency, multiplier, or ratio.
_NUMBER = re.compile(
    r"\d+(?:[.,]\d+)?\s?%|[$€£]\s?\d|\b\d+(?:[.,]\d+)?x\b|\bx\d+\b|"
    r"\b\d+\s+(?:in|out of)\s+\d+\b", re.IGNORECASE)
# Internal/cosmetic churn — not newsworthy on its own.
_TRIVIAL = re.compile(
    r"\b(?:chore|docs?|typo|readme|refactor\w*|lint\w*|\bci\b|cleanup|"
    r"bump|dependenc\w*|internal|whitespace|formatting|comment[s]?)\b",
    re.IGNORECASE)

# Bad news — a rough keyword detector for events you would NOT want to celebrate
# (incident, breach, recall, layoffs, price hike). Not truth, not severity — just
# "check the mood".
#
# In two reaches, and the split is the whole design.
#
# It began as one list read over the whole body, on the stated trade that a false
# flag is cheaper than posting a cheerful graphic during an outage. That trade is
# real, but it only holds while the flag is occasional. On a live account every
# lead the product had ever produced came back flagged — go-ethereum's routine
# 5 KB maintenance notes matched twice, on "failing" and "shutdown", both past
# character 2000 inside the list of merged pull requests. A warning that fires on
# everything is not read, and the dialog standing in front of it is something to
# click past. Over-firing stops being the cheap direction at exactly the point it
# stops warning anybody.
#
# Which is the same thing `_SIGNAL_CHARS` below already records for the admitting
# rules: in an aggregated changelog an ordinary word is a certainty, not a signal.
# So the words that are ordinary changelog vocabulary — a test fails, a process
# crashes, an option is deprecated, a database shuts down — count only where the
# post would be about, the title and the opening of the notes. Words that cannot
# mean anything else keep the whole body, because an incident report that buries
# "we were breached" on page two is the case this exists for.
_BAD_NEWS_ANYWHERE = re.compile(
    r"\b(?:incident|outage|breach\w*|hacked|hackers?|hacking|"
    r"ransom\w*|phishing|malware|scam|fraud\w*|"
    r"recall(?:s|ed|ing)?|lawsuit|sued|"
    r"layoffs?|lay off|laid off|redundan\w*|bankrupt\w*|"
    r"backlash|controvers\w*|scandal|"
    r"price (?:increase|hike|rise)|raising prices)\b",
    re.IGNORECASE)
# "hackathon" and "hackable" are not a hack, so the strong list spells out the
# three forms that are, rather than taking every word starting "hack".
_BAD_NEWS_UP_FRONT = re.compile(
    r"\b(?:down(?:time)?|offline|disrupt\w*|degrad\w*|"
    r"exploit\w*|vulnerab\w*|cve|leak\w*|exposed|"
    r"settlement|fine[ds]?|penalt\w*|investigat\w*|"
    r"shut ?down|shutting down|more expensive|"
    r"delay\w*|postpon\w*|discontinu\w*|deprecat\w*|sunset\w*|"
    r"apolog\w*|sorry|regret|complaint\w*|fail\w*|broke\w*|crash\w*)\b",
    re.IGNORECASE)


def detect_bad_news(item: FetchedItem) -> bool:
    """True when an item reads as negative/sensitive — worth a warning before
    posting. See the two lists above for what each reach is for.

    `_SIGNAL_CHARS` is defined further down beside the rules that made the same
    discovery first; the window is deliberately the same one.
    """
    title, body = item.title or "", item.body or ""
    if _BAD_NEWS_ANYWHERE.search(f"{title}\n{body}"):
        return True
    return bool(_BAD_NEWS_UP_FRONT.search(f"{title}\n{body[:_SIGNAL_CHARS]}"))


def _is_dev_prerelease(item: FetchedItem) -> bool:
    """True for a nightly/alpha/canary/dev build. We classify from the title/tag,
    not `raw['prerelease']` — GitHub sets that flag for milestone betas too, and
    those (beta/rc/preview) are intentionally kept worthy."""
    tag = str((item.raw or {}).get("tag_name") or "")
    return bool(_DEV_PRERELEASE.search(f"{item.title or ''} {tag}"))


#: How much of an item's body the ADMITTING rules are allowed to read.
#:
#: Measured before it was chosen. The rules were run over real releases from
#: supabase, PostHog, prisma, grafana, next.js and sentry — eight items each.
#: next.js's canaries and grafana's 135-byte patches came out right. sentry was
#: 8 of 8 "worthy", every one for the same reason, on bodies of 26 KB to 125 KB:
#: in an aggregated changelog a word like "limit" or "support for" is a
#: certainty, not a signal. Reading the whole body meant the rules lost their
#: power to discriminate exactly as the changelog grew — on precisely the
#: companies this product is for.
#:
#: So a signal counts when it sits where the post would be about: the title and
#: the opening of the release notes. Page 40 of a changelog is not the news.
#:
#: The asymmetry is deliberate. Rules that ADMIT an item read this window,
#: because a false "worthy" spends the reader's trust on junk and the feed stops
#: being worth opening. Rules that REFUSE or WARN are cheaper to over-fire than
#: to miss, and keep the whole text: the trivial anti-rule's escape, `_SECURITY`,
#: and the unambiguous half of `detect_bad_news`.
#:
#: The other half of `detect_bad_news` reads this window too, and the reason is
#: written beside it: over-firing is only the cheap direction while it stays
#: occasional. Measured on a live account it was every item, on words like
#: "failing" deep in a commit list — the same certainty-not-signal this window
#: was cut for.
_SIGNAL_CHARS = 1200


def _normalise(title: str) -> str:
    return " ".join((title or "").lower().split())


def score_item(item: FetchedItem, recent_titles: Iterable[str]) -> tuple[str, str]:
    """Classify one item as ("worthy"|"weak"|"duplicate", reason).

    `recent_titles` is whatever the caller considers "already seen" (the poller
    passes the last ~30 days; the demo passes items scored earlier this run).
    """
    title = (item.title or "").strip()
    norm = _normalise(title)
    if not norm:
        return ("weak", "no title to judge")

    if norm in {_normalise(t) for t in recent_titles}:
        return ("duplicate", "a matching item was already seen recently")

    body = (item.body or "").strip()
    text = f"{title}\n{body}"
    head = f"{title}\n{body[:_SIGNAL_CHARS]}"

    # Anti-rule: internal/cosmetic churn with nothing customer-facing.
    if _TRIVIAL.search(title) and not _IMPACT.search(head):
        return ("weak", "looks like an internal or cosmetic change")

    # Anti-rule: a dev-channel pre-release (nightly/alpha/canary) — continuous
    # churn a company doesn't post about. Milestone pre-releases stay eligible.
    if _is_dev_prerelease(item):
        return ("weak", "a pre-release / dev-channel build — not usually post-worthy")

    signals: list[str] = []
    if (m := _IMPACT.search(head)):
        signals.append(f'affects customers — "{m.group(0).strip()}"')
    if (m := _NUMBER.search(head)):
        signals.append(f'carries a concrete number — "{m.group(0).strip()}"')
    if (m := _CHANGE.search(head)):
        signals.append(f'describes a before→after change — "{m.group(0).strip()}"')
    if (m := _LAUNCH.search(head)):
        signals.append(f'a launch — "{m.group(0).strip()}"')
    # Security reads the WHOLE body — see _SIGNAL_CHARS. It is also a signal in
    # its own right, not only an escape from the patch rule below: an item whose
    # single newsworthy fact sits at the bottom of a long changelog would
    # otherwise be demoted for having nothing near the top.
    if (m := _SECURITY.search(text)):
        signals.append(f'mentions security — "{m.group(0).strip()}"')

    # Anti-rule: a semver patch (x.y.Z, Z>0) needs a STRONG signal to be worthy —
    # a number, a before→after change, a launch, or security. A patch riding only
    # the generic customer-impact keyword is churn; demote it. (Hypothesis test 2.)
    if _SEMVER_PATCH.search(title) and not _SECURITY.search(text):
        strong = _NUMBER.search(head) or _CHANGE.search(head) or _LAUNCH.search(head)
        if not strong:
            return ("weak", "patch release with no strong signal")

    if signals:
        return ("worthy", "; ".join(dict.fromkeys(signals)))
    return ("weak", "no strong newsworthiness signal")
