"""What REQUIRE_VERIFIED_EMAIL actually changes, written down and enforced.

The flag defaults to false, so every guard behind it is inert in development and
in the whole test suite — and turns live the day somebody sets it in prod. That
is a bad shape: the code path that only exists in production is the one nobody
runs. It has already cost us once. `POST /api/onboarding/first-post` carried the
dependency, which meant nothing while the flag was off; the day it went on, every
newly registered account reached the last screen of onboarding and was told
"Please verify your email before publishing" — on a screen that publishes
nothing — instead of the post the product had promised it. 1600 green tests, all
of them running with the flag off.

So the inventory is a test rather than a paragraph in a document. Two directions,
and both matter:

  * every route that IS gated is in the list below, so turning the flag on has no
    surprises — you can read what changes;
  * every route that PUBLISHES is gated, so the list cannot quietly develop a
    hole on the side that matters.

The second one found `/api/media/{id}/publish-x` the first time it ran.
"""
import pytest
from fastapi.routing import APIRoute

from api.deps import require_verified
from main import app

#: Everything an unverified cloud account is refused once the flag is on.
#:
#: The shared property is that each one puts content in front of the public under
#: somebody's name — which is what an unconfirmed address makes unattributable.
#: Scheduling is here because it is publishing with a delay.
#:
#: Deliberately NOT here: generating, editing, adapting, rendering a reel,
#: uploading, connecting accounts, and the onboarding sample post. Those cost us
#: money or cost the user nothing, and none of them reaches an audience — gating
#: them buys no protection and takes the product away from somebody who has not
#: opened their mail client yet.
GATED_ROUTES = {
    ("POST", "/api/posts/{post_id}/publish"),
    ("POST", "/api/posts/{post_id}/schedule"),
    ("POST", "/api/posts/{post_id}/publish-reel"),
    ("POST", "/api/posts/{post_id}/publish-video"),
    ("POST", "/api/media/{asset_id}/publish-x"),
}

#: Routes whose path says publish or schedule and whose effect does not.
#:
#: Each one is here for a reason worth keeping: cancelling and looking must never
#: need a verified address. Somebody who cannot receive our mail — a typo, a dead
#: forwarder — must still be able to stop a scheduled post and see what happened
#: to it, or the gate turns a wrong address into content they cannot recall.
NOT_A_PUBLISH = {
    ("POST", "/api/settings/publish/test"),      # calls the network to check keys
    ("DELETE", "/api/posts/{post_id}/schedule"),  # unschedules — the way out
    ("GET", "/api/publish-jobs"),                 # reads job status
    ("GET", "/api/publish-jobs/{job_id}"),        # reads job status
}


def _uses_require_verified(route: APIRoute) -> bool:
    """Whether the dependency is anywhere in this route's dependency tree.

    Walked rather than matched on the decorator line, because a route can pick it
    up from its router or from another dependency, and a grep over source would
    miss both.
    """
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if id(dep) in seen:
            continue
        seen.add(id(dep))
        if dep.call is require_verified:
            return True
        stack.extend(dep.dependencies)
    return False


def _flatten(routes):
    """Every APIRoute the app serves, including the ones inside routers.

    `app.routes` does not contain them directly: this FastAPI keeps each
    `include_router` as an `_IncludedRouter` wrapper holding the real router in
    `original_router`. A walk that only looks for APIRoute at the top level finds
    four — /health, /terms, /privacy and the SPA fallback — and every assertion
    below then passes over an empty set. That is the third vacuous-green of the
    day, and the most dangerous kind here: an inventory that reports "nothing
    ungated" because it looked at nothing.
    """
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        else:
            inner = getattr(r, "original_router", None)
            yield from _flatten(getattr(inner, "routes", None)
                                or getattr(r, "routes", []))


def _routes():
    for r in _flatten(app.routes):
        for method in r.methods - {"HEAD", "OPTIONS"}:
            yield method, r.path, r


def test_the_flag_changes_exactly_these_routes():
    """The list is the answer to "what happens if I turn it on?"."""
    actual = {(m, p) for m, p, r in _routes() if _uses_require_verified(r)}
    assert actual == GATED_ROUTES, (
        f"gated but undocumented: {sorted(actual - GATED_ROUTES)}; "
        f"documented but ungated: {sorted(GATED_ROUTES - actual)}")


def test_everything_that_publishes_is_gated():
    """The direction that finds holes rather than surprises.

    `/api/media/{asset_id}/publish-x` was one: an unverified account could not
    publish a post, and could publish a video from the library to X — the same
    act, answered differently depending on which screen it was reached from.
    """
    publishing = {
        (m, p) for m, p, _ in _routes()
        if ("publish" in p or "schedule" in p) and (m, p) not in NOT_A_PUBLISH
    }
    ungated = {
        (m, p) for m, p, r in _routes()
        if (m, p) in publishing and not _uses_require_verified(r)
    }
    assert not ungated, f"these publish without a verified email: {sorted(ungated)}"


def test_the_inventory_is_actually_looking_at_the_app():
    """Guards the guards. Every assertion in this file is a statement about a
    set of routes, and all of them pass trivially if that set comes back empty —
    which is exactly what happens if the walk into the routers breaks. So the
    walk itself is asserted first."""
    paths = {p for _, p, _ in _routes()}
    assert len(paths) > 50, f"only found {len(paths)} routes — the walk is broken"
    assert "/api/posts/{post_id}/publish" in paths


@pytest.mark.parametrize("method,path", sorted(GATED_ROUTES))
def test_each_documented_route_still_exists(method, path):
    """A list that names a route nobody serves any more is worse than no list:
    it reads as coverage."""
    assert (method, path) in {(m, p) for m, p, _ in _routes()}
