"""What we hand to a third party when error monitoring is on.

Sentry has been wired since the cloud deploy and switched off ever since — the
DSN is empty in production, so nothing has ever been sent and nothing has ever
been checked. Two things about that wiring are worth pinning down before it is
switched on, because both are invisible until the day they matter.

**Which deployment, and which build.** `sentry_sdk.init(dsn=..., traces=...)`
with nothing else produces a stream of errors that cannot be attributed: a
laptop and production land in the same feed, and there is no way to ask "did
this start with the last deploy?" — which is the first question anybody asks.

**What travels with the error.** This app holds captions people have not
published, brand profiles, and encrypted third-party keys. `send_default_pii`
defaults to False in the SDK, but a default is a thing that changes in a minor
release of somebody else's library; here it is stated, and asserted, so turning
it on becomes a deliberate act with a red test in the way.
"""
import pytest

from config import Settings
from main import sentry_options


def _settings(**over) -> Settings:
    fields = dict(app_mode="cloud", sentry_dsn="https://key@example.ingest.sentry.io/1")
    fields.update(over)
    return Settings(**fields)


def test_no_dsn_means_no_monitoring(monkeypatch):
    """The off state, and the one production has been in all along. Nothing is
    initialised, so nothing can leave the machine."""
    assert sentry_options(_settings(sentry_dsn="")) is None


def test_the_dsn_is_passed_through(monkeypatch):
    assert sentry_options(_settings())["dsn"] == "https://key@example.ingest.sentry.io/1"


def test_errors_say_which_deployment_they_came_from(monkeypatch):
    """Without this a laptop and production share one feed, and the first
    triage question — is this even real users? — has no answer."""
    assert sentry_options(_settings(app_mode="cloud"))["environment"] == "cloud"
    assert sentry_options(_settings(app_mode="local"))["environment"] == "local"


def test_errors_say_which_build_they_came_from(monkeypatch):
    """"Did this start with the last deploy?" is unanswerable without a release,
    and it is the question that decides whether to roll back."""
    monkeypatch.setenv("APP_RELEASE", "c0f3a36")

    assert sentry_options(_settings())["release"] == "c0f3a36"


def test_an_unstamped_build_says_so_rather_than_guessing(monkeypatch):
    """A missing release is honest. A wrong one — say, a stale constant — sends
    somebody to read a diff that never shipped."""
    monkeypatch.delenv("APP_RELEASE", raising=False)

    assert "release" not in sentry_options(_settings())


def test_personal_data_is_not_attached_to_errors(monkeypatch):
    """The guard this file exists for. This app holds unpublished captions,
    brand profiles and encrypted third-party keys; none of that belongs in a
    monitoring vendor's database because a request happened to 500."""
    assert sentry_options(_settings())["send_default_pii"] is False


@pytest.mark.parametrize("mode", ["cloud", "local"])
def test_monitoring_is_configured_the_same_way_everywhere(monkeypatch, mode):
    """Desktop crashes are the ones nobody reports. If monitoring is ever turned
    on there it must not quietly send more than the server does."""
    assert sentry_options(_settings(app_mode=mode))["send_default_pii"] is False


def test_tracing_is_off_until_somebody_asks_for_it():
    """The plan gives 5,000 errors a month and the project was created with
    tracing switched off. A sample rate above zero would ship spans nobody
    accepts and nobody reads — and would be discovered, if ever, as a bill."""
    assert sentry_options(_settings())["traces_sample_rate"] == 0.0
