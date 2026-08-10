"""The net that catches a CSP violation, tested on itself.

The `page` fixture already fails a test on any uncaught JS exception, and that
has caught half the bugs this suite exists for. It cannot catch this one.

A CSP-blocked inline handler throws nothing. The browser declines to compile it,
dispatches a `securitypolicyviolation` event, writes a line to the console, and
carries on. The button is simply inert. `pageerror` stays silent, and the test
fails only if some later assertion happens to depend on that button — which,
for the ~60 handlers in this app that no browser test touches, it never will.

So the fixture gains a second collector. And a collector nobody has seen fire is
forty lines that might do nothing, which is why this file exists: it puts a real
violation in front of it and demands the report.
"""
import pytest

pytestmark = pytest.mark.e2e


def _probe(page, live_server, body: str) -> None:
    """Navigate to a page we control, rather than rewriting the current one.

    `set_content` would be the obvious way and it silently defeats the thing
    under test: it does `document.open()`, which strips every listener on the
    document — including the collector. A real navigation is also what happens
    in life, and it is what re-runs the init script.
    """
    page.route("**/csp-probe", lambda r: r.fulfill(
        status=200, content_type="text/html", body=body))
    page.goto(f"{live_server}/csp-probe")
    page.wait_for_timeout(300)


def test_the_collector_sees_a_violation(page, live_server, csp_violations):
    """A policy that forbids images, and an image. The browser must tell us."""
    _probe(page, live_server,
           '<meta http-equiv="Content-Security-Policy" content="img-src \'none\'">'
           '<img src="/static/vendor/barlow-400.woff2">')

    seen = csp_violations()      # reading drains, so teardown stays green
    assert any("img-src" in v for v in seen), seen


def test_an_unclaimed_violation_fails_the_test_that_caused_it(tmp_path):
    """The guard the whole phase rests on, and the one no ordinary test can
    reach: both tests above drain, so deleting the teardown assertion leaves
    them green. This one provokes a violation and never claims it, then asserts
    that the run FAILED — which means running pytest inside pytest, because a
    teardown failure cannot be caught from within the test it belongs to.
    """
    import subprocess
    import sys
    from pathlib import Path

    probe = Path(__file__).parent / "_probe_unclaimed_violation.py"
    probe.write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.e2e\n"
        "def test_leaves_a_violation_behind(page, live_server):\n"
        "    page.route('**/csp-probe', lambda r: r.fulfill(\n"
        "        status=200, content_type='text/html',\n"
        '        body=\'<meta http-equiv="Content-Security-Policy" '
        "content=\\\"img-src &#39;none&#39;\\\">'\n"
        "             '<img src=\"/static/vendor/barlow-400.woff2\">'))\n"
        "    page.goto(f'{live_server}/csp-probe')\n"
        "    page.wait_for_timeout(300)\n",
        encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:warnings",
             "-m", "e2e", "--tb=line"],   # the reason has to reach stdout
            capture_output=True, text=True, timeout=600,
            cwd=str(Path(__file__).parent.parent.parent))
    finally:
        probe.unlink(missing_ok=True)

    assert r.returncode != 0, r.stdout[-2000:]
    assert "CSP violations" in r.stdout, r.stdout[-2000:]


def test_a_clean_page_reports_nothing(page, live_server, csp_violations):
    """The other half of the claim. A collector that always fires is as useless
    as one that never does — and this one is asserted at teardown, so a false
    positive would fail every test in the suite."""
    _probe(page, live_server, "<p>nothing to see</p>")

    assert csp_violations() == []
