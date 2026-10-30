"""
OptiMind's own update check. About once a day it asks GitHub which release is newest on
OptiMind's release page (OPTIMIND_UPDATE_REPO, "owner/name", set in the build) and says
whether it's newer than this version. Settings → About and Home then link to the release
page, where you download the new Setup yourself.

It only reads: nothing is downloaded or installed, and nothing about the PC is sent (GitHub
gets an ordinary request, as when a browser opens the page). Without a release page set,
it doesn't check at all.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from app import __version__, i18n
from app.config import DATA_DIR, settings
from app.i18n import tx

logger = logging.getLogger("optimind.updates")

_STATE_PATH = DATA_DIR / "update_check.json"
_CHECK_SECONDS = 24 * 3600.0
_RETRY_SECONDS = 3600.0  # offline, or GitHub didn't answer: try again in an hour
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_HEADERS = {"User-Agent": f"OptiMind-AI/{__version__} (update check)", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}
_lock = threading.Lock()
_running = threading.Lock()


def repo() -> str | None:
    """The release page's "owner/name", or None when the build has none (or a malformed one)."""
    value = (settings.optimind_update_repo or "").strip().strip("/")
    return value if _REPO.match(value) else None


def version_tuple(text: str | None) -> tuple[int, int, int] | None:
    """"v1.5.0" or "1.5.0" -> (1, 5, 0); anything else (a pre-release name too) -> None."""
    match = _VERSION.match((text or "").strip())
    return tuple(int(g) for g in match.groups()) if match else None


def parse_release(data: dict, name: str) -> dict:
    """GitHub's answer about the newest release: its version, name, date, page, and that
    version's Setup file (OptiMind-AI-Setup-<version>.exe) with its SHA-256 as GitHub lists it,
    never another file on the page: release 2.0.0 also had every older Setup attached, and the
    first one, 1.1.0's, was shown as the update. Refuses a tag that isn't a version and a page
    outside the release page."""
    tag = str(data.get("tag_name") or "")
    latest = version_tuple(tag)
    if latest is None:
        raise ValueError(tx("the newest release's tag ({tag}) isn't a version like v1.5.0", tag=tag[:40]))
    page = str(data.get("html_url") or "")
    if not page.startswith(f"https://github.com/{name}/releases/"):
        raise ValueError(tx("GitHub's answer pointed outside the release page"))
    setup_name = f"optimind-ai-setup-{'.'.join(map(str, latest))}.exe"
    setup = next((a for a in data.get("assets") or [] if isinstance(a, dict)
                  and str(a.get("name") or "").lower() == setup_name), None)
    digest = str((setup or {}).get("digest") or "")
    return {
        "latest": ".".join(map(str, latest)), "name": str(data.get("name") or tag)[:120],
        "published": str(data.get("published_at") or "")[:10] or None, "url": page,
        "setup": {"name": str(setup["name"])[:200], "size": int(setup.get("size") or 0),
                  "sha256": digest[7:].upper() if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) else None}
        if setup else None,
    }


def _load() -> dict:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def status() -> dict:
    """The last check's answer (kept across restarts) next to this version."""
    name = repo()
    if not name:
        return {"enabled": False, "current": __version__}
    with _lock:
        saved = i18n.restored(_load())
    if saved.get("repo") != name:  # the release page changed with this build: that answer isn't about it
        saved = {}
    latest, current = version_tuple(saved.get("latest")), version_tuple(__version__)
    return {"enabled": True, "repo": name, "current": __version__, "page": f"https://github.com/{name}/releases",
            "newer": bool(latest and current and latest > current),
            **{key: saved.get(key) for key in ("latest", "name", "published", "url", "setup", "checked_at", "error")}}


def stale() -> bool:
    """Time to ask again: a day after an answer, an hour after an error."""
    with _lock:
        saved = _load()
    if saved.get("repo") != repo():
        return True
    keep = _RETRY_SECONDS if saved.get("error") else _CHECK_SECONDS
    return time.time() - float(saved.get("checked_ts") or 0) > keep


def check() -> dict:
    """Asks GitHub now for the newest release of the release page, and keeps the answer."""
    name = repo()
    if not name:
        return status()
    result = {"repo": name, "checked_at": datetime.now(timezone.utc).isoformat(), "checked_ts": time.time(),
              "error": None}
    try:
        import httpx

        r = httpx.get(f"https://api.github.com/repos/{name}/releases/latest", headers=_HEADERS, timeout=15,
                      follow_redirects=True)
        if urllib.parse.urlsplit(str(r.url)).hostname != "api.github.com":
            raise ValueError(tx("the answer came from somewhere other than GitHub"))
        if r.status_code == 404:
            raise ValueError(tx("no version is published on the release page yet"))
        if r.status_code != 200:
            raise ValueError(tx("GitHub answered {code}", code=r.status_code))
        result.update(parse_release(r.json(), name))
    except ValueError as e:
        error = i18n.message(e)
        result["error"] = error if isinstance(error, i18n.Text) else error[:200]
    except Exception as e:
        result["error"] = tx("couldn't reach GitHub ({error})", error=type(e).__name__)
    with _lock:
        saved = _load()
        if result["error"] and saved.get("repo") == name:  # offline for a while: what the last answer said stays
            result = {**{key: saved[key] for key in ("latest", "name", "published", "url", "setup") if key in saved},
                      **result}
        try:
            _STATE_PATH.write_text(json.dumps(i18n.storable(result), indent=1), encoding="utf-8")
        except OSError:
            logger.debug("Couldn't save the update check", exc_info=True)
    return status()


def refresh_in_background() -> None:
    """A check on a thread of its own when the last one is old: the page never waits for GitHub."""
    if not repo() or not stale() or not _running.acquire(blocking=False):
        return

    def run() -> None:
        try:
            check()
        except Exception:
            logger.debug("Update check failed", exc_info=True)
        finally:
            _running.release()

    threading.Thread(target=run, name="optimind-update-check", daemon=True).start()
