"""How many inline event handlers are left, exactly.

`script-src 'self'` without `'unsafe-inline'` refuses to compile `onclick="…"`,
and the refusal is silent — no exception, no console entry the browser suite
reads, just a control that stopped working. Browser tests cannot be the oracle
for that: they name only about half the handler-bearing elements in the file, so
roughly sixty handlers would go dark with every test still green.

This one reads the files on disk instead, which makes it total. It is also the
ratchet: the counts below come down with each commit that converts a screen, and
they are asserted EXACTLY rather than as an upper bound. `<=` is not
mutation-safe — raising the constant leaves it green, which is precisely the
edit somebody makes when a conversion turns out to be awkward.

When both reach zero, `'unsafe-inline'` comes out of the policy and that change
is a no-op by construction.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"

#: An `on*=` attribute in markup. Matches prose too, which is why the one
#: comment that discussed `onerror` was reworded rather than special-cased here:
#: a counter with an exception list is a counter nobody trusts.
_HANDLER = re.compile(r"\son[a-z]+=")

#: Lowered by each conversion commit. Not `<=`; see the module docstring.
EXPECTED = {
    "index.html": 0,     # the static markup — CSP phase 4
    "app.js": 0,         # HTML built in JS — CSP phase 3
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_inline_handler_count(name):
    found = _HANDLER.findall((STATIC / name).read_text(encoding="utf-8"))
    assert len(found) == EXPECTED[name], (
        f"{name}: expected {EXPECTED[name]} inline handlers, found {len(found)}: "
        f"{sorted(set(found))}")


def test_the_two_wiring_conventions_never_meet():
    """`data-act` is a LOCAL marker: a render function looks the element up and
    assigns `el.onclick = () => f(value)`, so the argument rides in a closure and
    CSP never sees it. `data-action` is a GLOBAL registry key handled by one
    delegated listener.

    An element carrying both would fire twice — once from its own closure and
    once from the registry — and only on the screens where a local marker name
    happens to collide with a registry key. Intermittent, screen-specific, and
    invisible in review. Two attributes, one rule, and it cannot happen.
    """
    both = re.findall(r"<[^>]*\bdata-act=[^>]*\bdata-action=[^>]*>|"
                      r"<[^>]*\bdata-action=[^>]*\bdata-act=[^>]*>",
                      (STATIC / "index.html").read_text(encoding="utf-8")
                      + (STATIC / "app.js").read_text(encoding="utf-8"))
    assert not both, both


def _registry_keys() -> set[str]:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block = app.split("const ACTIONS = Object.freeze({", 1)[1].split("\n});", 1)[0]
    return set(re.findall(r"^\s*'([a-z0-9-]+)':", block, re.M))


def _used_actions() -> set[str]:
    text = ((STATIC / "index.html").read_text(encoding="utf-8")
            + (STATIC / "app.js").read_text(encoding="utf-8"))
    return set(re.findall(r"data-(?:action|change|input)=[\"']\$?\{?([a-z0-9-]+)\}?[\"']", text))


def test_every_action_is_wired_both_ways():
    """Both directions, because each catches a different rename.

    A `data-action` with no entry is a dead control — the click dispatches to
    nothing and the button looks broken. An entry with no `data-action` is dead
    code that reads as coverage: it will sit in the registry looking like the
    feature still works long after the markup stopped asking for it.
    """
    keys, used = _registry_keys(), _used_actions()
    assert used - keys == set(), f"markup asks for actions that do not exist: {used - keys}"
    assert keys - used == set(), f"registry entries nothing uses: {keys - used}"
