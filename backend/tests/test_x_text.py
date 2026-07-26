"""Tweet-length enforcement: cut on word boundaries, never invent filler tweets."""
import pytest

from models.schemas import TWEET_CHAR_LIMIT
from services.x_text import (
    append_tags, clamp_count, enforce_parts, fit_tweet, looks_truncated, tweet_length,
)


def test_short_text_is_untouched():
    assert fit_tweet("Short and sweet.") == "Short and sweet."


def test_fit_respects_the_limit():
    assert len(fit_tweet("word " * 200)) <= TWEET_CHAR_LIMIT


def test_fit_never_splits_a_word():
    """The old `text[:250]` produced things like '...filter cof'.

    Checked by whole-token membership: substring checks are useless here because
    a fragment like 'cof' is a substring of 'coffee'.
    """
    text = ("Single-origin coffee reveals terroir and complexity that blends mask, "
            "which is why filter brewing rewards it. " * 4)
    source_words = set(text.split())
    out = fit_tweet(text)

    assert len(out) <= TWEET_CHAR_LIMIT
    body = out.rstrip("…").rstrip()
    assert body, "cut away everything"
    # every token, especially the last one, must be a complete source word
    for token in body.split():
        assert token in source_words, f"{token!r} is a fragment, not a whole word"


def test_fit_handles_one_giant_word():
    """No boundary exists — cutting inside the token is the only option."""
    out = fit_tweet("a" * 500)
    assert len(out) <= TWEET_CHAR_LIMIT
    assert out.endswith("…")


def test_fit_trims_dangling_punctuation():
    text = "This sentence runs long, " * 20
    assert not fit_tweet(text).rstrip("…").endswith(",")


# ── URLs cost 23 characters, whatever their length ───────────────────────────
# X rewrites every link to a t.co of exactly 23 chars. Counting the real string
# meant a post carrying a long source URL was trimmed ~70 characters shorter than
# it needed to be — and then the link was often stripped anyway.

def test_a_url_costs_twenty_three_characters():
    long_url = "https://example.com/" + "segment/" * 12
    assert len(long_url) > 90                       # the raw string really is long
    assert tweet_length(long_url) == 23


def test_tweet_length_matches_len_without_urls():
    assert tweet_length("plain text") == len("plain text")


def test_fit_keeps_a_tweet_that_only_overflows_on_raw_length():
    """200 chars of prose plus a 120-char link is 250 as X counts it — publishing
    it untouched is correct, trimming it is the bug."""
    url = "https://example.com/" + "x" * 100
    text = "w" * 200 + " " + url
    assert len(text) > TWEET_CHAR_LIMIT             # raw length would reject it
    assert fit_tweet(text) == text


def test_append_tags_counts_urls_the_same_way():
    url = "https://example.com/" + "y" * 100
    text = "w" * 180 + " " + url
    out = append_tags(text, "#dev", limit=TWEET_CHAR_LIMIT)
    assert "…" not in out                           # body survived intact
    assert out.endswith("#dev")


# ── a cut should read as a finished thought ──────────────────────────────────

def test_fit_prefers_a_finished_sentence_and_drops_the_ellipsis():
    """A complete sentence is not a truncation, so it must not be advertised as
    one. Drafts ending "…for a more…" are what prompted this."""
    text = ("We shipped faster builds. Exports now run in the background. "
            "The editor gained a command palette. ") * 3
    out = fit_tweet(text)
    assert len(out) <= TWEET_CHAR_LIMIT
    assert not out.endswith("…")
    assert out.rstrip().endswith(".")


def test_fit_does_not_gut_a_tweet_for_an_early_full_stop():
    """Without a floor, "Hi. " + 240 chars would collapse to "Hi." — technically a
    finished sentence, practically a deleted post."""
    text = "Hi. " + "word " * 100
    out = fit_tweet(text)
    assert len(out) > TWEET_CHAR_LIMIT * 0.5


def test_clamp_trims_above_max():
    assert len(clamp_count([f"t{i}" for i in range(12)], 3, 7)) == 7


def test_clamp_does_not_pad_below_min():
    """Padding to hit a number is what breaks thread coherence — a 2-tweet answer
    to a narrow topic is correct."""
    assert clamp_count(["one", "two"], 5, 7) == ["one", "two"]


def test_clamp_drops_blanks():
    assert clamp_count(["a", "  ", "", "b"], 2, 7) == ["a", "b"]


@pytest.mark.asyncio
async def test_enforce_leaves_short_parts_alone_and_skips_the_model():
    calls = []

    async def shorten(text, limit):
        calls.append(text)
        return "should not be used"

    parts = ["fine", "also fine"]
    assert await enforce_parts(parts, shorten) == parts
    assert calls == []                      # no needless spend on compliant tweets


@pytest.mark.asyncio
async def test_enforce_prefers_the_model_rewrite():
    async def shorten(text, limit):
        return "A tight rewrite that fits."

    out = await enforce_parts(["x " * 300], shorten)
    assert out == ["A tight rewrite that fits."]


@pytest.mark.asyncio
async def test_enforce_falls_back_when_rewrite_still_too_long():
    async def shorten(text, limit):
        return "still far too long " * 40

    out = await enforce_parts(["x " * 300], shorten)
    assert len(out[0]) <= TWEET_CHAR_LIMIT


@pytest.mark.asyncio
async def test_enforce_survives_a_failing_shortener():
    async def shorten(text, limit):
        raise RuntimeError("provider down")

    out = await enforce_parts(["y " * 300], shorten)
    assert len(out[0]) <= TWEET_CHAR_LIMIT   # deterministic cut still applied


@pytest.mark.asyncio
async def test_enforce_works_without_a_shortener():
    out = await enforce_parts(["z " * 300], None)
    assert len(out[0]) <= TWEET_CHAR_LIMIT


def test_looks_truncated():
    assert looks_truncated("cut here…")
    assert not looks_truncated("A complete thought.")


# ── append_tags: the hashtags are the part that must survive ────────────────

def test_append_tags_shortens_the_body_not_the_tags():
    """A cut inside "#FitnessOver40" publishes a different tag, so the text yields."""
    tags = "#FitnessOver40 #StrengthTraining"
    out = append_tags("word " * 80, tags)
    assert len(out) <= TWEET_CHAR_LIMIT
    assert out.endswith(tags)                 # every tag intact, none clipped
    assert "…" in out                         # the body is what gave way


def test_append_tags_leaves_a_fitting_tweet_alone():
    out = append_tags("Short and done.", "#Sleep")
    assert out == "Short and done.\n\n#Sleep"


def test_append_tags_without_tags_is_a_no_op():
    assert append_tags("Just the tweet.", "") == "Just the tweet."
    assert append_tags("Just the tweet.", "   ") == "Just the tweet."


def test_append_tags_skips_the_limit_for_long_form():
    """X Premium lifts the cap — a 900-char post must not be trimmed to make room."""
    body = "sentence. " * 90
    out = append_tags(body, "#Premium", limit=None)
    assert len(out) > TWEET_CHAR_LIMIT
    assert "…" not in out
