"""OptiMind AI's automated tests. Run them from the project folder with:

    python -m unittest

They never change your PC or your data: OptiMind runs in test mode (it only logs what
it would do), the AI providers are local or faked (no network), and every file it
writes goes to a temporary folder instead of data/, including the settings file the
Settings page saves (your .env is never read or written). This runs before any test
module imports the app, so the settings below win.
"""
import atexit
import logging
import os
import shutil
import tempfile

logging.disable(logging.WARNING)  # the app's own log lines would drown the test report
_DATA_DIR = tempfile.mkdtemp(prefix="optimind-tests-")
os.environ.update({
    "OPTIMIND_DRY_RUN": "true",
    "OPTIMIND_RUN_AS_ADMIN": "false",
    "OPTIMIND_AI_PROVIDERS": "local",
    "OPTIMIND_DATA_DIR": _DATA_DIR,
    "OPTIMIND_SETTINGS_FILE": os.path.join(_DATA_DIR, "settings.env"),
    "OPTIMIND_NO_DIALOGS": "true",  # OptiMind's own confirmations answer No, never shown (tests fake them)
    "OPTIMIND_UPDATE_REPO": "",  # no update check on the real release page (tests/test_updates.py fakes GitHub)
    "OPTIMIND_LANGUAGE": "en",  # the texts the tests expect, whatever Windows' language (tests/test_i18n.py: the others)
})
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)


def api_client(app):
    """A test client the API answers like OptiMind's window: from this PC, with the secret
    the agent gives its window (app/service/access.py)."""
    from fastapi.testclient import TestClient

    from app.service import access

    return TestClient(app, base_url="http://127.0.0.1:8765", headers={access.HEADER: access.KEY})
