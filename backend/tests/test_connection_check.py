"""The daily credential sweep.

Two things make or break this feature. It must not turn into a daily nag — one
email on the working→broken transition, then silence. And it must not destroy
what it cannot re-read: the sweep has no way to learn a token's expiry, so it
has to carry the stored estimate forward instead of blanking it.
"""
from types import SimpleNamespace

import pytest

import services.connection_check as cc
import services.email as email_mod
import services.publishing.factory as factory_mod


class _FakePublisher:
    """Either answers verify_credentials or raises, and records that it closed."""

    def __init__(self, info=None, error=None):
        self._info = info
        self._error = error
        self.closed = False

    async def verify_credentials(self):
        if self._error:
            raise self._error
        return self._info

    async def close(self):
        self.closed = True


class _FakeDB:
    def __init__(self, creds):
        self._creds = creds
        self.commits = 0

    async def get(self, _model, _pk):
        return self._creds

    async def commit(self):
        self.commits += 1


def _creds(health=None):
    return SimpleNamespace(user_id="u1", connection_health=health)


def _user(email="owner@example.com"):
    return SimpleNamespace(id="u1", email=email, is_active=True)


@pytest.fixture
def wired(monkeypatch):
    """Publisher factory + settings builder + email, all under test control."""
    state = SimpleNamespace(publishers={}, sent=[], made=[])

    def _make(platform, _settings, **_kw):
        state.made.append(platform)
        pub = state.publishers.get(platform)
        if pub is None:
            raise RuntimeError(f"{platform} credentials not configured")
        if isinstance(pub, Exception):
            raise pub
        return pub

    async def _settings(_db, _user):
        return SimpleNamespace()

    async def _send(to, platform, reason):
        state.sent.append((to, platform, reason))
        return True

    monkeypatch.setattr(factory_mod, "make_publisher_for", _make)
    monkeypatch.setattr(cc, "build_settings_for_user", _settings)
    monkeypatch.setattr(email_mod, "send_connection_broken_email", _send)
    return state


# ---------------------------------------------------------------- one network


async def test_a_working_network_is_recorded_with_its_handle(wired):
    wired.publishers["x"] = _FakePublisher(info={"handle": "acme"})
    record = await cc._check_one("x", None)
    assert record["ok"] is True
    assert record["handle"] == "acme"
    assert not record["error"]


async def test_a_failing_network_is_an_answer_not_an_exception(wired):
    wired.publishers["x"] = _FakePublisher(error=RuntimeError("401 bad token"))
    record = await cc._check_one("x", None)
    assert record["ok"] is False
    assert "401 bad token" in record["error"]


async def test_missing_credentials_count_as_not_ok(wired):
    """The factory refusing to build is a real answer: the network is unusable."""
    record = await cc._check_one("instagram", None)
    assert record["ok"] is False
    assert "not configured" in record["error"]


async def test_the_publisher_is_closed_even_when_verification_fails(wired):
    pub = _FakePublisher(error=RuntimeError("boom"))
    wired.publishers["x"] = pub
    await cc._check_one("x", None)
    assert pub.closed is True


# ---------------------------------------------------------------- per user


async def test_first_failed_check_notifies_the_owner(wired):
    creds = _creds()
    await cc.check_user(_FakeDB(creds), _user())
    assert creds.connection_health["x"]["ok"] is False
    assert {p for _to, p, _r in wired.sent} == {"instagram", "x"}


async def test_a_still_broken_connection_does_not_email_again(wired):
    """The whole point of gating on the transition: no daily nag."""
    creds = _creds()
    db = _FakeDB(creds)
    await cc.check_user(db, _user())
    wired.sent.clear()

    await cc.check_user(db, _user())
    assert wired.sent == []


async def test_a_connection_that_breaks_after_working_notifies_once(wired):
    creds = _creds()
    db = _FakeDB(creds)
    wired.publishers["x"] = _FakePublisher(info={"handle": "acme"})
    await cc.check_user(db, _user())
    wired.sent.clear()

    wired.publishers["x"] = _FakePublisher(error=RuntimeError("401 expired"))
    await cc.check_user(db, _user())
    assert [p for _to, p, _r in wired.sent] == ["x"]


async def test_the_sweep_keeps_a_known_expiry(wired):
    """It cannot read expiry back, so overwriting would erase what Renew learned."""
    creds = _creds({"instagram": {"ok": True, "expires_at": "2026-09-01T00:00:00+00:00",
                                  "expires_estimated": False}})
    wired.publishers["instagram"] = _FakePublisher(info={"username": "acme"})
    await cc.check_user(_FakeDB(creds), _user())
    ig = creds.connection_health["instagram"]
    assert ig["expires_at"] == "2026-09-01T00:00:00+00:00"
    assert ig["expires_estimated"] is False


async def test_a_user_with_no_credentials_row_is_skipped(wired):
    assert await cc.check_user(_FakeDB(None), _user()) == {}
    assert wired.sent == []


async def test_a_user_without_an_email_is_not_notified(wired):
    creds = _creds()
    await cc.check_user(_FakeDB(creds), _user(email=""))
    assert wired.sent == []
    assert creds.connection_health["x"]["ok"] is False   # still recorded
