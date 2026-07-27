"""Opt-in fact-checking for creator posts.

The Business module verifies a draft against the company page it was written
from. A creator post usually has no such source: it was generated from a topic.
The rule that makes this feature honest rather than decorative is therefore
negative — with nothing to check against, we say so and never ask the model,
because a model grading its own output from memory is not a fact-check.
"""
import pytest

from services import fact_check
from services.sources.base import FetchedItem


class _FakeFetcher:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error
        self.urls = []

    async def fetch(self, url, since=None):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.items


@pytest.fixture
def fetchers(monkeypatch):
    """Map url -> fetcher, so a test can make one URL fail and another work."""
    made = {}

    def _get(kind, *, ssl_verify=True):
        return made["_next"]

    monkeypatch.setattr(fact_check, "get_source_fetcher", _get)
    monkeypatch.setattr(fact_check, "detect_source_type", lambda url: "generic_page")
    return made


def _item(title="Pricing update", body="We cut prices by 20% in June."):
    return FetchedItem(external_id="1", kind="generic_page", title=title,
                       url="https://ex.com/1", published_at=None, body=body)


# ------------------------------------------------------------------ gathering


async def test_pasted_text_is_enough_and_fetches_nothing(fetchers):
    fetchers["_next"] = _FakeFetcher([_item()])
    text, used = await fact_check.gather_source_text(
        [], pasted="Our revenue doubled in Q2.")
    assert "revenue doubled" in text
    assert used == []
    assert fetchers["_next"].urls == []


async def test_a_cited_url_becomes_source_text(fetchers):
    fetchers["_next"] = _FakeFetcher([_item()])
    text, used = await fact_check.gather_source_text(["https://ex.com/1"])
    assert "Pricing update" in text and "cut prices by 20%" in text
    assert used == [{"url": "https://ex.com/1", "ok": True, "error": ""}]


async def test_an_unreachable_source_is_reported_not_swallowed(fetchers):
    """Silently checking against fewer sources would mark good claims unconfirmed."""
    fetchers["_next"] = _FakeFetcher(error=RuntimeError("404 Not Found"))
    text, used = await fact_check.gather_source_text(["https://ex.com/1"])
    assert text == ""
    assert used[0]["ok"] is False
    assert "404" in used[0]["error"]


async def test_only_http_urls_are_fetched(fetchers):
    """A citation is model output; file:// or javascript: must never reach a fetcher."""
    fetchers["_next"] = _FakeFetcher([_item()])
    text, used = await fact_check.gather_source_text(
        ["file:///etc/passwd", "javascript:alert(1)", "ftp://ex.com/x"])
    assert text == ""
    assert used == []
    assert fetchers["_next"].urls == []


async def test_the_number_of_sources_is_capped(fetchers):
    fetchers["_next"] = _FakeFetcher([_item()])
    urls = [f"https://ex.com/{i}" for i in range(10)]
    _text, used = await fact_check.gather_source_text(urls)
    assert len(used) == fact_check.MAX_SOURCE_URLS
    assert len(fetchers["_next"].urls) == fact_check.MAX_SOURCE_URLS


async def test_the_source_text_is_capped(fetchers):
    """A whole scraped site in the prompt is a cost and a context-window problem."""
    fetchers["_next"] = _FakeFetcher([_item(body="x" * 100000)])
    text, _used = await fact_check.gather_source_text(["https://ex.com/1"])
    assert len(text) <= fact_check.MAX_SOURCE_CHARS


async def test_duplicate_urls_are_fetched_once(fetchers):
    fetchers["_next"] = _FakeFetcher([_item()])
    await fact_check.gather_source_text(["https://ex.com/1", "https://ex.com/1"])
    assert fetchers["_next"].urls == ["https://ex.com/1"]


def test_material_means_enough_text_to_check_against():
    assert fact_check.has_material("  ") is False
    assert fact_check.has_material("See our site") is False
    assert fact_check.has_material("x" * fact_check.MIN_SOURCE_CHARS) is True
    # A one-line changelog entry is a real source and must qualify.
    assert fact_check.has_material("Pricing update — we cut prices by 20% in June.")


# ------------------------------------------------------------------ verdict


class _FakeProvider:
    def __init__(self, raw='[{"claim":"We cut prices by 20%","status":"confirmed",'
                           '"evidence":"We cut prices by 20% in June."}]'):
        self.raw = raw
        self.calls = 0

    async def generate_text(self, **kw):
        self.calls += 1
        return self.raw, []


async def test_no_source_means_no_verdict_and_no_model_call(fetchers):
    """The load-bearing rule. Without a source there is nothing to verify, and
    asking the model anyway would produce confident nonsense."""
    provider = _FakeProvider()
    result = await fact_check.verify_post(
        provider, draft_text="We cut prices by 20%.", source_urls=[], pasted="",
        text_model="m")
    assert result["status"] == "no_source"
    assert result["claims"] == []
    assert provider.calls == 0


async def test_a_grounded_claim_comes_back_confirmed(fetchers):
    fetchers["_next"] = _FakeFetcher([_item()])
    provider = _FakeProvider()
    result = await fact_check.verify_post(
        provider, draft_text="We cut prices by 20%.",
        source_urls=["https://ex.com/1"], pasted="", text_model="m")
    assert result["status"] == "checked"
    assert result["claims"][0]["status"] == "confirmed"
    assert result["sources_used"][0]["url"] == "https://ex.com/1"
    assert provider.calls == 1


async def test_an_invented_confirmation_is_downgraded(fetchers):
    """verify_claims re-checks evidence against the source; this pins that the
    creator path gets the same treatment and not a softer one."""
    fetchers["_next"] = _FakeFetcher([_item(body="Nothing about prices here at all.")])
    provider = _FakeProvider(
        '[{"claim":"We cut prices by 20%","status":"confirmed",'
        '"evidence":"We cut prices by 20% in June."}]')
    result = await fact_check.verify_post(
        provider, draft_text="We cut prices by 20%.",
        source_urls=["https://ex.com/1"], pasted="", text_model="m")
    assert result["claims"][0]["status"] == "unconfirmed"
    assert result["claims"][0]["evidence"] == ""


async def test_a_broken_model_is_an_error_not_a_clean_bill_of_health(fetchers):
    fetchers["_next"] = _FakeFetcher([_item()])
    provider = _FakeProvider(raw="not json at all")
    result = await fact_check.verify_post(
        provider, draft_text="We cut prices by 20%.",
        source_urls=["https://ex.com/1"], pasted="", text_model="m")
    assert result["status"] == "error"
    assert result["claims"] == []


async def test_unreachable_sources_do_not_become_a_silent_no_source(fetchers):
    """The user must be able to tell 'you gave me nothing' from 'your link died'."""
    fetchers["_next"] = _FakeFetcher(error=RuntimeError("timed out"))
    provider = _FakeProvider()
    result = await fact_check.verify_post(
        provider, draft_text="We cut prices.", source_urls=["https://ex.com/1"],
        pasted="", text_model="m")
    assert result["status"] == "no_source"
    assert result["sources_used"][0]["ok"] is False
    assert provider.calls == 0
