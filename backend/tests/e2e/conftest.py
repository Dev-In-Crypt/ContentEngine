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
    """uvicorn in its own process, on a throwaway database."""
    import urllib.error
    import urllib.request

    workdir = tmp_path_factory.mktemp("e2e")
    db = (workdir / "e2e.db").as_posix()
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
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                "the test server exited early:\n" + (proc.stdout.read() or ""))
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("the test server did not become healthy in time")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page(live_server, browser):
    """A fresh context per test: no cookies, no localStorage, no shared session."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    p = context.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.goto(live_server)
    yield p
    # A thrown exception in the SPA is a failure even when the assertions passed —
    # half the bugs this suite is for surface as a swallowed TypeError.
    context.close()
    assert not errors, f"uncaught JS errors: {errors}"


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
