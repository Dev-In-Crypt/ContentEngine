"""Shared slowapi limiter (per-IP). Imported by main (to register the handler +
app.state) and by routers (for @limiter.limit decorators).

headers_enabled is intentionally OFF: with it on, slowapi injects X-RateLimit-*
headers and therefore requires every decorated endpoint to declare a
`response: Response` parameter. Our auth endpoints return Pydantic models, so
enabling headers makes slowapi raise on a None response. 429-on-exceed still
works without the informational headers."""
import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Off only when explicitly asked, and only for a test harness: the browser suite
# runs the server in its own process, where the in-process fixture that unhooks
# the limiter cannot reach — and a dozen sign-ups from 127.0.0.1 otherwise trip
# the 5/minute registration limit and fail as unreadable UI timeouts.
# Anything other than a literal "false" leaves it on, so a typo cannot silently
# disable rate limiting in production.
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() != "false"

limiter = Limiter(key_func=get_remote_address, headers_enabled=False,
                  enabled=RATE_LIMIT_ENABLED)
