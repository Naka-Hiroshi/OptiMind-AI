"""OptiMind's own update check: what the release page's answer (GitHub's API, faked) says
about this version, what isn't trusted, when it asks again, and the API. Nothing goes over the
network; the answers are kept in the tests' temporary data folder."""
import json
import time
import unittest
from unittest import mock

import httpx

from app.service import updates
from tests import api_client

REPO = "someone/OptiMind-AI"
SHA = "AB" * 32


def release(tag="v1.6.0", page=None, digest=f"sha256:{SHA.lower()}"):
    return {"tag_name": tag, "name": f"OptiMind AI {tag}", "published_at": "2026-11-02T10:00:00Z",
            "html_url": page or f"https://github.com/{REPO}/releases/tag/{tag}",
            "assets": [{"name": "README.txt", "size": 4000, "digest": None},
                       {"name": f"OptiMind-AI-Setup-{tag.lstrip('v')}.exe", "size": 25_000_000, "digest": digest}]}


def answer(status=200, body=None, url=f"https://api.github.com/repos/{REPO}/releases/latest"):
    return mock.Mock(status_code=status, url=url, json=mock.Mock(return_value=body if body is not None else release()))


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(updates.settings, "optimind_update_repo", REPO),
                  mock.patch.object(updates, "__version__", "1.5.0")):
            p.start()
            self.addCleanup(p.stop)
        updates._STATE_PATH.unlink(missing_ok=True)
        self.addCleanup(updates._STATE_PATH.unlink, missing_ok=True)

    def check(self, response):
        with mock.patch("httpx.get", return_value=response) as get:
            return updates.check(), get

    def test_versions(self):
        self.assertEqual(updates.version_tuple("v1.10.2"), (1, 10, 2))
        self.assertEqual(updates.version_tuple("1.5.0"), (1, 5, 0))
        for text in ("1.5", "v1.6.0-beta", "nightly", "", None):
            self.assertIsNone(updates.version_tuple(text))

    def test_a_newer_version(self):
        out, get = self.check(answer())
        self.assertTrue(out["newer"])
        self.assertEqual((out["latest"], out["current"], out["published"]), ("1.6.0", "1.5.0", "2026-11-02"))
        self.assertEqual(out["url"], f"https://github.com/{REPO}/releases/tag/v1.6.0")
        self.assertEqual(out["setup"], {"name": "OptiMind-AI-Setup-1.6.0.exe", "size": 25_000_000, "sha256": SHA})
        self.assertEqual(get.call_args.args[0], f"https://api.github.com/repos/{REPO}/releases/latest")
        self.assertIn("OptiMind-AI/", get.call_args.kwargs["headers"]["User-Agent"])  # and nothing about the PC
        self.assertEqual(updates.status()["latest"], "1.6.0")  # kept

    def test_only_that_versions_setup(self):
        # Real release: 2.0.0 had every older Setup attached too, and 1.1.0's (the first) was shown as the update
        older = [{"name": f"OptiMind-AI-Setup-1.{n}.0.exe", "size": 19_000_000, "digest": f"sha256:{'cd' * 32}"}
                 for n in range(1, 6)]
        body = release()
        body["assets"] = older + body["assets"]
        out, _ = self.check(answer(body=body))
        self.assertEqual(out["setup"], {"name": "OptiMind-AI-Setup-1.6.0.exe", "size": 25_000_000, "sha256": SHA})
        body["assets"] = older + [{"name": "OptiMind-Setup.exe", "size": 1, "digest": f"sha256:{'ef' * 32}"}]
        out, _ = self.check(answer(body=body))
        self.assertTrue(out["newer"])
        self.assertIsNone(out["setup"])  # no Setup of that version: no file or SHA-256 shown, only the page

    def test_already_the_newest(self):
        for tag in ("v1.5.0", "v1.4.0"):
            with self.subTest(tag):
                out, _ = self.check(answer(body=release(tag)))
                self.assertFalse(out["newer"])
                self.assertIsNone(out["error"])

    def test_what_isnt_trusted(self):
        cases = ((answer(body=release(page="https://example.com/OptiMind-Setup.exe")), "outside the release page"),
                 (answer(body=release("nightly")), "isn't a version"),
                 (answer(url="https://example.com/latest"), "somewhere other than GitHub"),
                 (answer(status=404), "no version is published"),
                 (answer(status=403), "GitHub answered 403"),
                 (answer(body=release(digest="sha256:not-hex")), None))
        for response, error in cases:
            with self.subTest(error):
                out, _ = self.check(response)
                if error:
                    self.assertIn(error, out["error"])
                    self.assertFalse(out["newer"])
                else:
                    self.assertIsNone(out["setup"]["sha256"])  # a digest that isn't one isn't shown
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("offline")):
            self.assertEqual(updates.check()["error"], "couldn't reach GitHub (ConnectError)")

    def test_offline_keeps_what_it_knew(self):
        self.check(answer())  # 1.6.0 is out
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("offline")):
            out = updates.check()
        self.assertEqual((out["newer"], out["latest"], out["error"]), (True, "1.6.0", "couldn't reach GitHub (ConnectError)"))
        self.assertEqual(out["setup"]["sha256"], SHA)  # the notice and its link stay while offline
        self.assertFalse(updates.stale())  # and it asks again in an hour

    def test_no_release_page_no_check(self):
        for value in ("", "not a repo", "a/b/c"):
            with self.subTest(value), mock.patch.object(updates.settings, "optimind_update_repo", value), \
                    mock.patch("httpx.get") as get, mock.patch.object(updates.threading, "Thread") as thread:
                self.assertEqual(updates.check(), {"enabled": False, "current": "1.5.0"})
                updates.refresh_in_background()
                get.assert_not_called()
                thread.assert_not_called()

    def test_when_it_asks_again(self):
        self.check(answer())
        self.assertFalse(updates.stale())
        saved = json.loads(updates._STATE_PATH.read_text(encoding="utf-8"))
        updates._STATE_PATH.write_text(json.dumps({**saved, "checked_ts": time.time() - 25 * 3600}), encoding="utf-8")
        self.assertTrue(updates.stale())  # a day later
        self.check(answer(status=500))
        self.assertFalse(updates.stale())
        saved = json.loads(updates._STATE_PATH.read_text(encoding="utf-8"))
        updates._STATE_PATH.write_text(json.dumps({**saved, "checked_ts": time.time() - 61 * 60}), encoding="utf-8")
        self.assertTrue(updates.stale())  # after an error: an hour later
        with mock.patch.object(updates.settings, "optimind_update_repo", "someone/Another"):
            self.assertTrue(updates.stale())  # another release page: ask it
            self.assertIsNone(updates.status()["latest"])  # the old answer isn't about it

    def test_the_page_never_waits(self):
        with mock.patch.object(updates, "stale", return_value=False), mock.patch.object(updates.threading, "Thread") as thread:
            updates.refresh_in_background()
        thread.assert_not_called()
        with mock.patch.object(updates, "stale", return_value=True), mock.patch.object(updates.threading, "Thread") as thread:
            updates.refresh_in_background()
            updates._running.release()  # the faked thread never ran to release it
        thread.return_value.start.assert_called_once()

    def test_the_api(self):
        from app.main import app

        client = api_client(app)
        with mock.patch.object(updates, "refresh_in_background") as refresh, \
                mock.patch.object(updates, "status", return_value={"enabled": True, "newer": False}):
            self.assertEqual(client.get("/api/app/update").json(), {"enabled": True, "newer": False})
        refresh.assert_called_once()
        with mock.patch.object(updates, "check", return_value={"enabled": True, "newer": True}) as check:
            self.assertEqual(client.post("/api/app/update/check").json(), {"enabled": True, "newer": True})
        check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
