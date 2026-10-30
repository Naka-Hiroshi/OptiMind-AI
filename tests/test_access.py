"""Who may change things through the local API: OptiMind's own window, with this start's secret;
not other programs or other Windows accounts. Reading stays open to this PC, and opening the window
and quitting stay open for OptiMind's own processes. Nothing on the PC is changed."""
import os
import sys
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.desktop import shell, window
from app.main import app
from app.service import access

REFUSED = "Refused: only OptiMind's own window may change things."


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.anyone = TestClient(app, base_url="http://127.0.0.1:8765")  # a program on this PC, no secret

    def test_changes_need_the_secret(self):
        for headers in ({}, {access.HEADER: "wrong"}, {access.HEADER: ""}):
            with self.subTest(headers=headers):
                r = self.anyone.post("/api/focus", json={}, headers=headers)
                self.assertEqual((r.status_code, r.json()["detail"]), (403, REFUSED))
        self.assertEqual(self.anyone.put("/api/settings", json={}).status_code, 403)
        self.assertEqual(self.anyone.post("/api/tweaks/memory_integrity/apply").status_code, 403)
        # With it, the request goes on (to the usual checks: this empty body is invalid)
        self.assertEqual(self.anyone.post("/api/focus", json={}, headers={access.HEADER: access.KEY}).status_code, 422)
        self.assertFalse(access.allowed("POST", "/api/focus", "é" * 43))  # any text, no error

    def test_reading_stays_open(self):
        self.assertEqual(self.anyone.get("/api/app").status_code, 200)

    def test_opening_the_window_and_quitting_stay_open(self):
        # A second start of OptiMind and the installer (--quit) don't have the secret
        for path in ("/api/app/window", "/api/app/quit"):
            with self.subTest(path):
                self.assertEqual(self.anyone.post(path).status_code, 409)  # no desktop app in the tests

    def test_a_website_is_still_refused_first(self):
        r = self.anyone.post("/api/focus", json={}, headers={access.HEADER: access.KEY, "Origin": "http://example.com"})
        self.assertEqual(r.json()["detail"], "Refused: only OptiMind's own pages on this PC may use this API.")

    def test_the_secret_is_long(self):
        self.assertGreaterEqual(len(access.KEY), 40)

    def test_the_window_gets_it_and_hands_it_to_its_page(self):
        env = access.window_environment()
        self.assertEqual(env[access.ENVIRONMENT], access.KEY)
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))  # the rest as the agent's own
        with mock.patch.dict(os.environ, {access.ENVIRONMENT: "K"}):
            self.assertEqual(window.DesktopApi().key(), "K")

    def test_the_agent_starts_its_window_with_it(self):
        with mock.patch.object(shell.admin_rights, "is_elevated", return_value=False), \
                mock.patch.object(shell.subprocess, "Popen") as popen:
            shell._start_window_process(["OptiMind.exe", "--window"])
        self.assertEqual(popen.call_args.kwargs["env"][access.ENVIRONMENT], access.KEY)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_the_agent_as_administrator_hands_it_over_too(self):
        # As administrator, the agent starts its window with Explorer's normal rights (Windows' calls faked)
        dlls = {name: mock.MagicMock() for name in ("user32", "kernel32", "advapi32")}
        with mock.patch.object(shell.ctypes, "WinDLL", side_effect=lambda name, **kwargs: dlls[name]):
            shell._StandardRightsProcess(["OptiMind.exe", "--window"], access.window_environment())
        environment_block = dlls["advapi32"].CreateProcessWithTokenW.call_args.args[5]
        self.assertIn(f"{access.ENVIRONMENT}={access.KEY}\0", environment_block[:])

    def test_open_from_source_only(self):
        self.assertTrue(access._open_from_source(False, {"OPTIMIND_OPEN_API": "true"}))
        self.assertFalse(access._open_from_source(True, {"OPTIMIND_OPEN_API": "true"}))  # the installed app
        self.assertFalse(access._open_from_source(False, {}))
        with mock.patch.object(access, "_OPEN_FROM_SOURCE", True):
            self.assertTrue(access.allowed("POST", "/api/focus", None))


if __name__ == "__main__":
    unittest.main()
