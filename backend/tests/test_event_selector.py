"""Event selection rules (Phase 1) — the заготовка of hypothesis test 2 (doc §14).

Explainable rules only. The duplicate anti-rule is a mutation target: dropping it
lets a re-seen item slip through as worthy/weak, so the duplicate test would fail.
"""
from services.event_selector import score_item
from services.sources.base import FetchedItem


def _item(title, body="", raw=None):
    return FetchedItem(external_id="x", kind="rss", title=title, url="u",
                       published_at=None, body=body, raw=raw or {})


def test_customer_impact_is_worthy():
    strength, reason = score_item(_item("New pricing: Pro plan is now cheaper"), [])
    assert strength == "worthy"
    assert "customers" in reason


def test_concrete_number_is_worthy():
    strength, _ = score_item(_item("Uptime improved to 99.9% this quarter"), [])
    assert strength == "worthy"


def test_launch_is_worthy():
    strength, _ = score_item(_item("Introducing our new mobile app"), [])
    assert strength == "worthy"


def test_internal_churn_is_weak():
    strength, _ = score_item(_item("chore: bump dependencies and fix a typo in the README"), [])
    assert strength == "weak"


def test_trivial_word_with_customer_impact_stays_worthy():
    # "docs" alone is trivial, but customer-facing pricing wins.
    strength, _ = score_item(_item("docs: document the new pricing tiers and limits"), [])
    assert strength == "worthy"


def test_bland_update_is_weak():
    strength, _ = score_item(_item("Some reflections on our week"), [])
    assert strength == "weak"


def test_duplicate_against_recent_titles():
    strength, reason = score_item(_item("Launch of v2"), ["launch of v2"])
    assert strength == "duplicate"          # mutation guard: drop the dup rule → fails
    assert "already seen" in reason


def test_duplicate_is_case_and_space_insensitive():
    strength, _ = score_item(_item("  Launch   of  V2 "), ["launch of v2"])
    assert strength == "duplicate"


def test_empty_title_is_weak_not_crash():
    strength, _ = score_item(_item("   "), [])
    assert strength == "weak"


# --- Precision fixes from hypothesis test 2 (dev pre-releases + fixes-only patches) ---

def test_dev_prerelease_canary_is_weak():
    # A canary build with real impact words is STILL demoted — companies don't
    # post about nightly channels. Mutation guard: drop the dev-prerelease rule
    # and this becomes worthy.
    strength, reason = score_item(
        _item("webframework v16.3.0-canary.90", "Add support for new caching limits"), [])
    assert strength == "weak"
    assert "pre-release" in reason


def test_dev_prerelease_alpha_via_github_tag_is_weak():
    # GitHub-style: the channel lives in raw["tag_name"], not the title.
    strength, _ = score_item(
        _item("Release", "Add support for new resources",
              raw={"tag_name": "v22.4.0-alpha.4"}), [])
    assert strength == "weak"


def test_milestone_prerelease_beta_stays_worthy():
    # beta / rc / preview get announced → they remain worthy.
    strength, _ = score_item(_item("OurApp v2.0.0-beta.1 is now available"), [])
    assert strength == "worthy"


def test_milestone_prerelease_preview_stays_worthy():
    strength, _ = score_item(_item("Platform v3.1.0-preview.6 announced"), [])
    assert strength == "worthy"


def test_fixes_only_patch_is_weak():
    # A semver patch riding only the generic "support for" keyword is churn.
    # Mutation guard: drop the patch anti-rule → this becomes worthy.
    strength, reason = score_item(_item("sdk v1.2.3", "Add support for a new endpoint"), [])
    assert strength == "weak"
    assert "patch release" in reason


def test_security_patch_stays_worthy():
    # Security is the one thing that keeps a patch worthy on its own.
    strength, _ = score_item(
        _item("sdk v1.2.3", "Fixes a security vulnerability (CVE-2026-0001)"), [])
    assert strength == "worthy"


def test_patch_with_concrete_number_stays_worthy():
    strength, _ = score_item(_item("sdk v1.2.3", "Reduces cold start by 40%"), [])
    assert strength == "worthy"


def test_stable_minor_with_features_stays_worthy():
    # Regression: a stable minor (patch == 0) with features is untouched.
    strength, _ = score_item(_item("sdk v1.3.0", "Add support for webhooks and audit logs"), [])
    assert strength == "worthy"


def test_zero_padded_date_is_not_a_patch():
    # A zero-padded date "2026.07.21" must NOT be read as a semver patch — the
    # customer-impact signal wins. Mutation guard: with the old \d+ minor it would
    # match as a patch and be demoted to weak.
    strength, _ = score_item(_item("Pricing update 2026.07.21"), [])
    assert strength == "worthy"


# ── how much of a changelog the rules are allowed to read ───────────────────
#
# Measured against real releases before any of this was written: supabase,
# PostHog, prisma, grafana, next.js and sentry, eight items each. next.js's
# canaries and grafana's 135-byte patches were classified correctly. sentry was
# 8 of 8 "worthy", every one of them for the same reason — bodies of 26 KB to
# 125 KB, in which a word like "limit" or "support for" is a certainty rather
# than a signal. The rules read title + the ENTIRE body, so their power to
# discriminate collapses exactly as the changelog grows — on precisely the
# companies the product is for.

_FILLER = "Internal refactor of the build pipeline. " * 400      # ~16 KB


def test_a_signal_buried_deep_in_a_huge_changelog_does_not_carry_it():
    """The post would be about the top of the release notes, not about page 40.

    This is the sentry case: an aggregated changelog says everything somewhere,
    so "it says 'limit' somewhere" is not evidence of anything.
    """
    strength, reason = score_item(
        _item("26.7.0", body=_FILLER + "\nAdded support for higher rate limits."), [])
    assert strength == "weak", reason


def test_the_same_signal_at_the_top_is_still_worthy():
    """The other half — narrowing the window must not make the rule blind."""
    strength, _ = score_item(
        _item("26.7.0", body="Added support for higher rate limits.\n" + _FILLER), [])
    assert strength == "worthy"


def test_security_is_read_from_the_whole_changelog():
    """Deliberately asymmetric. A signal that ADMITS an item is bounded, because a
    false "worthy" spends the reader's trust on junk. Security is the exception:
    a missed security announcement costs more than an extra item in the feed, so
    it keeps the whole text — buried at the bottom still counts."""
    strength, _ = score_item(_item("7.9.1", body=_FILLER + "\nFixes CVE-2026-1234."), [])
    assert strength == "worthy"


def test_bad_news_is_still_read_from_the_whole_body():
    """The warning side stays over-eager on purpose (doc §9) — narrowing the
    admitting rules must not quietly narrow this one too."""
    from services.event_selector import detect_bad_news
    assert detect_bad_news(_item("26.7.0", body=_FILLER + "\nWe are investigating an outage."))


def test_the_reason_says_what_it_actually_matched():
    """"affects customers (price, limits, availability, security)" was printed for
    every one of the 33 worthy items in the calibration run — a sentence that
    names the category and never the evidence. A reason a human cannot check is
    not explainable, which was the whole premise of using rules."""
    _, reason = score_item(_item("New pricing for the Pro plan"), [])
    assert "customers" in reason
    assert "pricing" in reason.lower(), reason


def test_the_bare_word_release_is_not_a_signal_of_anything():
    """`_LAUNCH` already excludes it, with the reason written next to it: every
    changelog entry literally says "release", so the word carries no information.
    `_IMPACT` kept it, and the calibration run showed the consequence — prisma
    came back 8 of 8 worthy, every one of them on `affects customers "release"`.
    The same insight, applied to the rule it was missing from."""
    strength, reason = score_item(
        _item("v7.9.0", body="This release contains internal changes."), [])
    assert strength == "weak", reason
