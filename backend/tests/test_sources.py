"""Business source fetchers + detection (Phase 1).

Each fetcher turns a mocked HTTP response into normalised FetchedItems. The date
parse is a mutation target: a fetcher that loses published_at silently breaks the
recency filter, so the tests pin the parsed datetime.
"""
import pytest
from pytest_httpx import HTTPXMock

from services.sources import SourceFetchError, detect_source_type, get_source_fetcher
from services.sources.base import SourceRateLimited, parse_iso, strip_html
from services.sources.github import GitHubReleasesFetcher
from services.sources.feed import FeedFetcher
from services.sources.page import GenericPageFetcher
from datetime import datetime, timezone


# ── detection + helpers (pure) ───────────────────────────────────────────────

def test_detect_github_repo():
    assert detect_source_type("https://github.com/fastapi/fastapi") == "github_releases"
    assert detect_source_type("https://github.com/fastapi/fastapi/releases") == "github_releases"


def test_detect_feed():
    assert detect_source_type("https://blog.example.com/feed") == "rss"
    assert detect_source_type("https://example.com/index.atom") == "rss"


def test_detect_generic_fallback():
    assert detect_source_type("https://example.com/changelog") == "generic_page"
    assert detect_source_type("github.com") == "generic_page"   # no owner/repo


def test_parse_iso_and_strip_html():
    assert parse_iso("2026-07-01T12:00:00Z") == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_iso("nonsense") is None
    assert parse_iso("") is None
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_get_source_fetcher_rejects_unknown():
    with pytest.raises(SourceFetchError):
        get_source_fetcher("youtube")


# ── GitHub releases ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_github_fetch_parses_releases(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[
        {"id": 1, "tag_name": "v2.0", "name": "Version 2.0",
         "html_url": "https://github.com/o/r/releases/tag/v2.0",
         "published_at": "2026-07-01T12:00:00Z", "body": "Now 50% faster.", "draft": False},
        {"id": 2, "tag_name": "v1.0", "name": "", "html_url": "https://github.com/o/r/releases/tag/v1.0",
         "published_at": "2020-01-01T00:00:00Z", "body": "old", "draft": False},
    ])
    items = await GitHubReleasesFetcher().fetch("https://github.com/o/r")
    assert len(items) == 2
    top = items[0]
    assert top.title == "Version 2.0"
    assert top.url.endswith("/tag/v2.0")
    assert top.body == "Now 50% faster."
    # date parse is the mutation guard — a broken parser makes this None.
    assert top.published_at == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert items[1].title == "v1.0"          # empty name falls back to tag


@pytest.mark.asyncio
async def test_github_since_filters_old_releases(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[
        {"id": 1, "tag_name": "new", "published_at": "2026-07-01T00:00:00Z", "draft": False},
        {"id": 2, "tag_name": "old", "published_at": "2020-01-01T00:00:00Z", "draft": False},
    ])
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = await GitHubReleasesFetcher().fetch("https://github.com/o/r", since=since)
    assert [i.raw["tag_name"] for i in items] == ["new"]


@pytest.mark.asyncio
async def test_github_skips_drafts(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[
        {"id": 1, "tag_name": "draft", "draft": True},
        {"id": 2, "tag_name": "real", "published_at": "2026-07-01T00:00:00Z", "draft": False},
    ])
    items = await GitHubReleasesFetcher().fetch("https://github.com/o/r")
    assert [i.raw["tag_name"] for i in items] == ["real"]


def test_github_rejects_non_repo_url():
    with pytest.raises(SourceFetchError):
        GitHubReleasesFetcher()._owner_repo("https://github.com/onlyowner")


@pytest.mark.asyncio
async def test_github_http_error_becomes_source_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=404)
    with pytest.raises(SourceFetchError):
        await GitHubReleasesFetcher().fetch("https://github.com/o/r")


# ── GitHub: token + rate limit ───────────────────────────────────────────────
# Anonymous calls are capped at 60/hour PER IP, and on a server that IP is shared
# by every tenant — so the poller starts failing as soon as a few sources exist.
# A token raises it to 5,000. Tokens are injected directly here: get_settings is
# lru_cached, so patching the env without cache_clear() is a silent no-op.

@pytest.mark.asyncio
async def test_github_sends_authorization_when_token_set(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[])
    await GitHubReleasesFetcher(token="ghp_secret").fetch("https://github.com/o/r")
    sent = httpx_mock.get_requests()[0]
    assert sent.headers.get("Authorization") == "Bearer ghp_secret"


@pytest.mark.asyncio
async def test_github_sends_no_authorization_when_token_empty(httpx_mock: HTTPXMock):
    """A blank credential is worse than none — GitHub 401s on `Bearer `."""
    httpx_mock.add_response(json=[])
    await GitHubReleasesFetcher(token="").fetch("https://github.com/o/r")
    assert "Authorization" not in httpx_mock.get_requests()[0].headers


@pytest.mark.asyncio
async def test_github_rate_limit_403_is_distinct_from_a_private_repo_403(httpx_mock: HTTPXMock):
    """Both are 403. "Try again in an hour" and "this repo is private" must not
    look identical, or a working source gets marked dead and deleted."""
    httpx_mock.add_response(status_code=403,
                            headers={"X-RateLimit-Remaining": "0",
                                     "X-RateLimit-Reset": "1785054916"})
    with pytest.raises(SourceRateLimited):
        await GitHubReleasesFetcher(token="").fetch("https://github.com/o/r")


@pytest.mark.asyncio
async def test_github_forbidden_403_is_not_called_a_rate_limit(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=403, headers={"X-RateLimit-Remaining": "57"})
    with pytest.raises(SourceFetchError) as err:
        await GitHubReleasesFetcher(token="").fetch("https://github.com/o/r")
    assert not isinstance(err.value, SourceRateLimited)


@pytest.mark.asyncio
async def test_rate_limit_message_suggests_a_token_only_when_missing(httpx_mock: HTTPXMock):
    """Show the remedy to someone who can act on it; don't nag someone who already did."""
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1785054916"}
    httpx_mock.add_response(status_code=403, headers=headers)
    with pytest.raises(SourceRateLimited) as anon:
        await GitHubReleasesFetcher(token="").fetch("https://github.com/o/r")
    assert "GITHUB_TOKEN" in str(anon.value)

    httpx_mock.add_response(status_code=403, headers=headers)
    with pytest.raises(SourceRateLimited) as with_token:
        await GitHubReleasesFetcher(token="ghp_x").fetch("https://github.com/o/r")
    assert "GITHUB_TOKEN" not in str(with_token.value)


# ── RSS/Atom feed ────────────────────────────────────────────────────────────

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>New pricing tiers</title>
    <link>https://ex.com/pricing</link>
    <guid>g1</guid>
    <pubDate>Wed, 01 Jul 2026 12:00:00 GMT</pubDate>
    <description>We changed &lt;b&gt;prices&lt;/b&gt;.</description>
  </item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_feed_fetch_parses_entries(httpx_mock: HTTPXMock):
    httpx_mock.add_response(content=_RSS.encode(), headers={"content-type": "application/rss+xml"})
    items = await FeedFetcher().fetch("https://ex.com/feed")
    assert len(items) == 1
    it = items[0]
    assert it.title == "New pricing tiers"
    assert it.url == "https://ex.com/pricing"
    assert "prices" in it.body and "<b>" not in it.body   # HTML flattened
    assert it.published_at.year == 2026 and it.published_at.month == 7


@pytest.mark.asyncio
async def test_feed_http_error_becomes_source_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=500)
    with pytest.raises(SourceFetchError):
        await FeedFetcher().fetch("https://ex.com/feed")


# ── Generic page ─────────────────────────────────────────────────────────────

_HTML = """<html><body>
  <h2 id="v2">Version 2.0 released</h2>
  <p>Now 50% faster and cheaper.</p>
  <h2>Bug fixes</h2>
  <p>Small stuff.</p>
</body></html>"""


@pytest.mark.asyncio
async def test_page_fetch_splits_on_headings(httpx_mock: HTTPXMock):
    httpx_mock.add_response(text=_HTML, headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 2.0 released", "Bug fixes"]
    assert items[0].url.endswith("#v2")            # heading id → anchor
    assert "faster" in items[0].body
    assert items[0].published_at is None           # generic pages have no per-item date


@pytest.mark.asyncio
async def test_page_http_error_becomes_source_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=503)
    with pytest.raises(SourceFetchError):
        await GenericPageFetcher().fetch("https://ex.com/changelog")


# ── Generic page: site chrome is not an event ────────────────────────────────
# Taking every h1-h3 in the document turned nav labels into "news": a live run
# produced a draft reading "The company is sharing a changelog to communicate
# updates" from the menu item "All changelog posts". Worse, a nav item titled
# "Pricing" matches the selector's _IMPACT rule and scores *worthy*, so furniture
# outranks real releases. Filtering belongs here, not in the selector: the poller
# writes a Lead for every non-duplicate item including weak ones.

def _page(body: str) -> str:
    return f"<html><body>{body}</body></html>"


@pytest.mark.asyncio
async def test_page_skips_nav_and_footer_headings(httpx_mock: HTTPXMock):
    httpx_mock.add_response(text=_page("""
      <nav><h2>Pricing</h2><h2>Docs</h2></nav>
      <h2>Version 3.0 released</h2><p>Now with exports.</p>
      <footer><h2>Company</h2></footer>"""), headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_keeps_a_heading_inside_an_article_header(httpx_mock: HTTPXMock):
    """<article><header><h2>…</h2></header> is standard blog-card markup — dropping
    every <header> would delete real entries along with the page banner."""
    httpx_mock.add_response(text=_page("""
      <header><h1>Our Blog</h1></header>
      <article><header><h2>Version 3.0 released</h2></header><p>Details.</p></article>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_scopes_to_main_when_present(httpx_mock: HTTPXMock):
    httpx_mock.add_response(text=_page("""
      <div class="sidebar"><h3>Recent posts</h3><h3>Categories</h3></div>
      <main><h2>Version 3.0 released</h2><p>Details.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_falls_back_to_the_document_without_main(httpx_mock: HTTPXMock):
    """Anti-over-filtering guard: plenty of real changelogs are a bare <div> soup.
    Losing them would be a worse failure than the furniture we're removing."""
    httpx_mock.add_response(text=_page("""
      <div><h2>Version 3.0 released</h2><p>Details.</p></div>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_ignores_an_empty_main(httpx_mock: HTTPXMock):
    """A <main> that holds no headings is a layout wrapper, not the content region.
    Scoping to it blindly would return nothing for the whole page."""
    httpx_mock.add_response(text=_page("""
      <main><p>Welcome.</p></main>
      <div><h2>Version 3.0 released</h2><p>Details.</p></div>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_drops_a_linked_heading_with_no_body(httpx_mock: HTTPXMock):
    """A heading that is only a link into another page is navigation, not an event.
    This is what "All changelog posts" actually is."""
    httpx_mock.add_response(text=_page("""
      <main>
        <h2><a href="/changelog/all">All changelog posts</a></h2>
        <h2>Version 3.0 released</h2><p>Details.</p>
      </main>"""), headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_keeps_an_unlinked_heading_with_a_short_body(httpx_mock: HTTPXMock):
    """Restates the pinned test's rule so nobody "optimises" it away: a terse entry
    with no anchor is a real entry. Body length is not evidence of furniture."""
    httpx_mock.add_response(text=_page("""
      <main><h2>Bug fixes</h2><p>Small stuff.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Bug fixes"]


@pytest.mark.asyncio
async def test_page_drops_the_page_title_heading(httpx_mock: HTTPXMock):
    """<h1>Changelog</h1> on /changelog is the page's own name, not an update."""
    httpx_mock.add_response(text=_page("""
      <main><h1>Changelog</h1>
      <h2>Version 3.0 released</h2><p>Details.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_keeps_a_first_h1_that_is_not_the_page_name(httpx_mock: HTTPXMock):
    """The title rule is narrow (h1 + first + matches the slug), not "drop any h1"."""
    httpx_mock.add_response(text=_page("""
      <main><h1>Version 3.0 released</h1><p>Details.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_drops_a_heading_with_nothing_under_it(httpx_mock: HTTPXMock):
    """A heading immediately followed by another heading groups them — it is a
    label ("New features", "Updates"), not an entry. Note EMPTY, not short: the
    pinned fixture's "Bug fixes" has a body and must survive."""
    httpx_mock.add_response(text=_page("""
      <main><h2>New features</h2>
      <h3>Version 3.0 released</h3><p>Details.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_drops_the_page_title_from_a_nested_url(httpx_mock: HTTPXMock):
    """Real case: raycast.com/changelog/windows — the page name matches a segment
    in the middle of the path, not the last one."""
    httpx_mock.add_response(text=_page("""
      <h1>Changelog</h1><p>What's new.</p>
      <h2>Version 3.0 released</h2><p>Details.</p>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog/windows")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_drops_a_link_list_label(httpx_mock: HTTPXMock):
    """Real case: unkey.com/changelog — "All changelog posts" heads the list of
    OTHER posts, so it has a fat body and no link of its own. Structure can't see
    it; the phrase is unambiguous navigation."""
    httpx_mock.add_response(text=_page("""
      <main><h2>All changelog posts</h2><p>Jan Feb Mar Apr entries…</p>
      <h2>Version 3.0 released</h2><p>Details.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]


@pytest.mark.asyncio
async def test_page_dedupes_repeated_headings(httpx_mock: HTTPXMock):
    httpx_mock.add_response(text=_page("""
      <main><h2>Version 3.0 released</h2><p>Details.</p>
      <h2>Version 3.0 released</h2><p>Mirrored below.</p></main>"""),
        headers={"content-type": "text/html"})
    items = await GenericPageFetcher().fetch("https://ex.com/changelog")
    assert [i.title for i in items] == ["Version 3.0 released"]
