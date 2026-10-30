"""
Who may change things through OptiMind's local API.

The agent runs with administrator rights, so its API refuses websites (app/main.py checks
Host and Origin) and, for anything that changes something, every program but OptiMind's own
window: such a request must carry this start's secret, which the agent hands only to the
window it starts (in the window's environment, never through the API). Other programs that
call 127.0.0.1, and other Windows accounts on this PC, can read but not change. Two requests
stay open because other OptiMind processes make them: opening the window (a second start of
OptiMind) and quitting (the installer, before an update).

From source, OPTIMIND_OPEN_API=true at start lifts the secret, for trying the API with plain
uvicorn; the installed app ignores it.
"""
from __future__ import annotations

import hmac
import os
import secrets

from app.config import FROZEN

HEADER = "X-OptiMind-Key"
ENVIRONMENT = "OPTIMIND_WINDOW_KEY"  # how the window gets the secret from the agent that starts it
KEY = secrets.token_urlsafe(32)  # new at every start
OPEN_PATHS = frozenset({"/api/app/window", "/api/app/quit"})
_READING = frozenset({"GET", "HEAD", "OPTIONS"})


def _open_from_source(frozen: bool, environ) -> bool:
    return not frozen and environ.get("OPTIMIND_OPEN_API", "").lower() in ("1", "true")


_OPEN_FROM_SOURCE = _open_from_source(FROZEN, os.environ)


def allowed(method: str, path: str, presented: str | None) -> bool:
    """Whether the API may do this request: reading, one of the two open requests, or the secret."""
    if method.upper() in _READING or path in OPEN_PATHS or _OPEN_FROM_SOURCE:
        return True
    return bool(presented) and hmac.compare_digest(presented.encode("utf-8", "replace"), KEY.encode())


def window_environment() -> dict[str, str]:
    """The environment for OptiMind's window: the agent's own, plus the secret."""
    return {**os.environ, ENVIRONMENT: KEY}
