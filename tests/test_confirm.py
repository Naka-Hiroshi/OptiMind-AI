"""OptiMind's own confirmation for the riskiest changes: a Windows dialog the agent shows itself,
No by default, on top of every window, giving up after 90 seconds; the tweak that turns off a
security feature and installing drivers from a backup wait for it. Windows' dialog is faked and
nothing on the PC is changed."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from app.desktop import confirm
from app.tools import driver_actions, tweaks
from tests import api_client


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class DialogTests(unittest.TestCase):
    def ask(self, answer):
        user32 = mock.MagicMock()
        user32.MessageBoxTimeoutW.return_value = answer
        with mock.patch.dict(os.environ, {"OPTIMIND_NO_DIALOGS": ""}), \
                mock.patch.object(confirm, "_user32", return_value=user32):
            result = confirm.ask("Apply X?", "Why it matters.")
        return result, user32.MessageBoxTimeoutW.call_args.args

    def test_only_yes_confirms(self):
        for answer, expected in ((6, True), (7, False), (32000, False)):  # Yes, No, no answer in time
            with self.subTest(answer=answer):
                self.assertIs(self.ask(answer)[0], expected)

    def test_no_by_default_on_top_and_it_gives_up(self):
        _, (owner, text, caption, flags, language, milliseconds) = self.ask(7)
        self.assertTrue(flags & 0x100)  # No is the default button
        self.assertTrue(flags & 0x40000)  # on top of every window
        self.assertEqual(milliseconds, 90_000)
        self.assertIn("Apply X?", text)
        self.assertEqual(caption, "OptiMind AI: please confirm")

    def test_the_tests_never_see_one(self):
        with mock.patch.object(confirm, "_user32") as user32:
            self.assertFalse(confirm.ask("Apply X?", "Why."))  # OPTIMIND_NO_DIALOGS: No, nothing shown
        user32.assert_not_called()


class RiskiestChangesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = api_client(app)

    def test_the_security_tweak_waits_for_your_yes(self):
        with mock.patch.object(tweaks, "apply", return_value="applied (checked)") as apply:
            with mock.patch.object(confirm, "ask", return_value=False) as asked:
                r = self.client.post("/api/tweaks/memory_integrity/apply")
            self.assertEqual((r.status_code, r.json()["detail"]),
                             (409, "Not applied: it wasn't confirmed in OptiMind's own dialog."))
            self.assertIn("Memory integrity", asked.call_args.args[0])
            apply.assert_not_called()
            with mock.patch.object(confirm, "ask", return_value=True):
                self.assertEqual(self.client.post("/api/tweaks/memory_integrity/apply").status_code, 200)
            apply.assert_called_once_with("memory_integrity")

    def test_other_tweaks_dont_ask(self):
        safe = next(t.id for t in tweaks.TWEAKS if t.risk == "safe")
        with mock.patch.object(tweaks, "apply", return_value="applied (checked)"), \
                mock.patch.object(confirm, "ask") as asked:
            self.assertEqual(self.client.post(f"/api/tweaks/{safe}/apply").status_code, 200)
        asked.assert_not_called()

    def ready(self):
        return [mock.patch("app.api.drivers_routes.is_elevated", return_value=True),
                mock.patch("app.api.drivers_routes.games.status", return_value={"current": None})]

    def test_a_driver_restore_waits_for_your_yes(self):
        folder = Path("D:/OptiMind driver backup/2026-10-20 10.00")
        patches = self.ready() + [mock.patch.object(driver_actions, "known_backup", return_value=folder)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch("app.api.drivers_routes.driver_jobs.start", return_value={"id": 1}) as start:
            with mock.patch.object(confirm, "ask", return_value=False):
                r = self.client.post("/api/drivers/backups/restore", json={"path": str(folder)})
            self.assertEqual((r.status_code, r.json()["detail"]),
                             (409, "Not started: it wasn't confirmed in OptiMind's own dialog."))
            start.assert_not_called()
            with mock.patch.object(confirm, "ask", return_value=True):
                self.assertEqual(self.client.post("/api/drivers/backups/restore", json={"path": str(folder)}).status_code,
                                 200)
            start.assert_called_once()

    def test_a_folder_that_isnt_a_backup_is_refused_before_asking(self):
        patches = self.ready() + [mock.patch.object(driver_actions, "backups", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(confirm, "ask") as asked:
            r = self.client.post("/api/drivers/backups/restore", json={"path": "C:\\Somewhere\\else"})
        self.assertEqual(r.status_code, 404)
        asked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
