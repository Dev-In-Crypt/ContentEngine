"""A real browser against a real server.

Everything else in this suite talks to the app through TestClient, which never
executes a line of the 5.6k-line SPA. These tests exist for the class of defect
that only appears once the JavaScript runs: a message written and then wiped by a
re-render, a modal that never opens, a guard that lets a bad value through.

The server runs in a **subprocess**, not a thread. Two reasons, both learned the
hard way: its configuration is environment variables, and setting those in-process
leaks `APP_MODE=cloud` and a throwaway DATABASE_URL into every other test in the
session; and an event loop spinning in the test process fights Playwright's sync
API for the greenlet, which on Windows ends in an access violation rather than a
failure you can read.

No API keys and no outbound network are needed — every path exercised here either
needs no model (the wizard's guards) or asserts the failure we get without one.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent.parent

pytest.importorskip("playwright", reason="playwright is not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """uvicorn in its own process, on a throwaway database.

    Its output goes to a **file**, never to an unread `subprocess.PIPE`. A pipe
    nobody reads is not a place output disappears into; it is a 4 KB buffer, and
    the writer blocks forever once it fills. The server would then still accept
    connections and answer none of them, so every later test died in `page.goto`
    with a navigation timeout and none of them with a readable failure. Alembic
    alone spends ~3 KB of that buffer on migration lines before the first test
    runs, which is why the whole suite wedged partway through while every file
    passed alone: only the long run reached the limit.
    """
    import urllib.error
    import urllib.request

    workdir = tmp_path_factory.mktemp("e2e")
    db = (workdir / "e2e.db").as_posix()
    log_path = workdir / "server.log"
    port = _free_port()

    env = {
        **os.environ,
        "APP_MODE": "cloud",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "SECRET_KEY": "e2e-secret-key-not-a-real-one-0123456789",
        "SSL_VERIFY": "false",
        # Nothing here may reach a provider or send mail.
        "OPENROUTER_API_KEY": "",
        "RESEND_API_KEY": "",
        "SENTRY_DSN": "",
        "REQUIRE_VERIFIED_EMAIL": "false",
        # Every test signs up from 127.0.0.1; the 5/minute registration limit
        # would otherwise turn the later ones into unreadable click timeouts.
        "RATE_LIMIT_ENABLED": "false",
        "PYTHONPATH": str(BACKEND),
    }
    logfile = log_path.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=logfile, stderr=subprocess.STDOUT,
    )

    def _log() -> str:
        """What the server has said so far, for a failure that needs explaining."""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return f"(no server log at {log_path})"
        return f"--- server log ({log_path}) ---\n{text}" if text else \
            f"(the server logged nothing; {log_path})"

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("the test server exited early:\n" + _log())
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            proc.kill()
            raise RuntimeError(
                "the test server did not become healthy in time:\n" + _log())
    except BaseException:
        logfile.close()
        raise

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    logfile.close()


#: Collects Content-Security-Policy violations in the page itself.
#:
#: An init script rather than a console listener, for two reasons: it runs before
#: any page script and survives navigation, so nothing is missed in the window
#: where the SPA boots; and it reads the event's own fields instead of matching
#: Chromium's console wording, which is not a contract.
_CSP_COLLECTOR = """
document.addEventListener('securitypolicyviolation', e => {
  (window.__cspViolations ||= []).push(
    e.violatedDirective + ' blocked ' + (e.blockedURI || '(inline)'));
});
"""


@pytest.fixture
def page(live_server, browser):
    """A fresh context per test: no cookies, no localStorage, no shared session."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    # Must be registered on the CONTEXT before the first page exists, or the
    # violations raised while index.html is parsed are gone before anything is
    # listening — and those are exactly the ones a policy change causes.
    context.add_init_script(_CSP_COLLECTOR)
    # Everything up to the yield has to clean up after itself. A fixture that
    # raises before yielding never runs its teardown, so a failing `goto` used to
    # strand the whole context — browser processes and all — and a run that lost
    # the server stranded one per remaining test. That turned a readable first
    # failure into a machine slowed by a pile of orphaned browsers.
    try:
        p = context.new_page()
        errors: list[str] = []
        p.on("pageerror", lambda e: errors.append(str(e)))
        p.goto(live_server)
    except BaseException:
        context.close()
        raise
    yield p
    # A thrown exception in the SPA is a failure even when the assertions passed —
    # half the bugs this suite is for surface as a swallowed TypeError.
    #
    # A CSP violation is the other half, and it is the quieter one: the browser
    # refuses to run something and throws nothing at all, so the only symptom is
    # a control that stopped working. Read before the context closes.
    violations = _read_violations(p)
    context.close()
    assert not errors, f"uncaught JS errors: {errors}"
    assert not violations, f"CSP violations: {violations}"


@pytest.fixture
def csp_violations(page):
    """Read the CSP violations so far — and take responsibility for them.

    Reading DRAINS the list, so a test that deliberately provokes a violation
    does not then fail its own teardown. Anything raised after the last read is
    still unclaimed and still fails, which keeps the default strict: a test has
    to say out loud that it expected one.
    """
    def _drain() -> list[str]:
        try:
            return page.evaluate(
                "(() => { const v = window.__cspViolations || []; "
                "window.__cspViolations = []; return v; })()") or []
        except Exception:
            return []
    return _drain


def _read_violations(p) -> list[str]:
    """Whatever the collector saw, or nothing if the page is already gone.

    A test that closed the page itself, or navigated somewhere unreachable, must
    not turn into a teardown error about the collector — that would hide the
    real failure behind a report about the machinery watching for it.
    """
    try:
        return p.evaluate("window.__cspViolations || []") or []
    except Exception:
        return []


@pytest.fixture
def signup(page, live_server):
    """Register a fresh account through the real API and land in the app."""
    def _go(email: str = "", account_type: str = "creator") -> str:
        addr = email or f"e2e-{int(time.time() * 1000000)}@example.com"
        page.evaluate(
            """async ({addr, kind}) => {
                const r = await fetch('/api/auth/register', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email: addr, password: 'password123',
                                          account_type: kind}),
                });
                const d = await r.json();
                localStorage.setItem('api_token', d.access_token);
            }""",
            {"addr": addr, "kind": account_type},
        )
        page.goto(live_server)
        return addr
    return _go


@pytest.fixture
def signed_in(page, signup):
    """A fresh account with first-run setup already out of the way.

    Deliberately click-based rather than seeding the "already done" flag: it
    means every suite in the repo exercises the escape hatch on every test,
    which is free coverage of the one control that must never break.

    HOW it is dismissed lives in nav.py — this fixture only says that it is.
    """
    # Imported here, not at module scope: the importorskip above is what lets
    # this package be collected on a machine with no playwright installed, and
    # a top-level playwright import would run before it and defeat it.
    from tests.e2e.nav import dismiss_onboarding

    def _go(account_type: str = "creator") -> str:
        addr = signup(account_type=account_type)
        dismiss_onboarding(page)
        return addr
    return _go
