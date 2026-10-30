"""Removing activity entries (Agent → Activity): which entries a request picks, the log
file and the list the dashboard shows. Uses a log file of its own."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app import i18n
from app.agent import logger
from app.main import app
from app.models import ActionLogEntry, ActionType
from app.service.state import app_state
from tests import api_client

START = datetime(2026, 10, 17)  # UTC without a time zone, like the log


def entry(hours: int, action=ActionType.LOWER_PRIORITY, target="app.exe", applied=True, result="ok",
          reason="Busy in the background.") -> ActionLogEntry:
    return ActionLogEntry(timestamp=START + timedelta(hours=hours), action=action, target=target, reason=reason,
                          applied=applied, result=result)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "action_log.jsonl"
        patcher = mock.patch.object(logger, "ACTION_LOG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, app_state, "recent_actions", app_state.recent_actions)
        # In UTC−3: Oct 16 10 PM and 11 PM, then Oct 17 8 PM, 10 PM and 11 PM
        self.entries = [
            entry(1),
            entry(2, target="game.exe", applied=False, result="blocked: a game is in focus"),
            entry(23, action=ActionType.APPLY_TWEAK, target="Some tweak", applied=False, result="Access denied",
                  reason="Changed by you from the Optimize tab."),
            entry(25),
            entry(26, applied=False, result="simulated (test mode)"),
        ]
        for e in self.entries:
            logger.log_action(e)
        app_state.recent_actions = list(self.entries)
        self.client = api_client(app)

    def remove(self, body: dict) -> int:
        r = self.client.post("/api/logs/remove", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["removed"]

    def in_file(self) -> list[str]:
        return [ActionLogEntry.model_validate_json(line).timestamp.isoformat()
                for line in self.path.read_text(encoding="utf-8").splitlines()]

    def listed(self) -> list[dict]:
        return self.client.get("/api/logs?limit=200").json()

    def assert_left(self, hours: list[int]) -> None:
        expected = [(START + timedelta(hours=h)).isoformat() for h in hours]
        self.assertEqual(self.in_file(), expected)
        self.assertEqual([e["timestamp"] for e in self.listed()], expected)

    def test_status_like_the_dashboard(self):
        self.assertEqual([logger.result_status(e) for e in self.entries],
                         ["Applied", "Blocked", "Error", "Applied", "Logged only"])
        self.assertEqual(logger.result_status(entry(0, applied=False, result="kept its own priority")), "Unchanged")
        self.assertEqual(logger.result_status(entry(0, applied=False, result="failed: gone")), "Error")

    def test_entries_as_the_dashboard_lists_them(self):
        """The page goes by these fields, never by the English of a result (shown in another language)."""
        trim = entry(0, action=ActionType.TRIM_WORKING_SET,
                     result="freed 1,536 MB of RAM from 1 instance (checked; Windows compresses or caches it until "
                            "the app needs it)")
        purge = ActionLogEntry(action=ActionType.PURGE_STANDBY_LIST, target=i18n.tx("system"), reason="RAM at 90%.",
                               applied=True, result="standby cache purged: free memory 1,000 MB -> 3,500 MB (checked)")
        shown = [logger.shown_entry(e) for e in (trim, purge, self.entries[2], self.entries[4])]
        self.assertEqual([s["freed_mb"] for s in shown], [1536.0, 2500.0, None, None])
        self.assertEqual([s["status"] for s in shown], ["Applied", "Applied", "Error", "Logged only"])
        self.assertEqual([s["by_you"] for s in shown], [False, False, True, False])
        self.assertEqual(shown[1]["target"], "system")  # what the page sends back to remove the entry
        self.assertNotIn("target_i18n", shown[1])
        self.assertEqual(i18n.shown(shown[1]["target_shown"], "pt-BR"), "sistema")

    def test_one_entry(self):
        blocked = self.listed()[1]  # as the dashboard sends it back
        key = {k: blocked[k] for k in ("timestamp", "action", "target")}
        self.assertEqual(self.remove({"entries": [key]}), 1)
        self.assert_left([1, 23, 25, 26])
        self.assertEqual(self.remove({"entries": [key]}), 0)  # already gone

    def test_one_day_in_local_time(self):
        day = {"since": "2026-10-17T00:00:00-03:00", "until": "2026-10-18T00:00:00-03:00"}
        self.assertEqual(self.remove(day), 3)
        self.assert_left([1, 2])

    def test_one_day_one_result(self):
        self.assertEqual(self.remove({"since": "2026-10-17T03:00:00Z", "until": "2026-10-18T03:00:00Z",
                                      "status": "Applied"}), 1)
        self.assert_left([1, 2, 23, 26])

    def test_one_result_on_every_day(self):
        self.assertEqual(self.remove({"status": "Error"}), 1)
        self.assert_left([1, 2, 25, 26])

    def test_everything(self):
        with self.path.open("a", encoding="utf-8") as f:
            f.write('{"action": "from_an_older_version"}\n')
        self.assertEqual(self.remove({"status": "Applied"}), 2)  # a choice keeps lines it can't read...
        self.assertIn("from_an_older_version", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.remove({"everything": True}), 4)  # ...everything doesn't
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.listed(), [])
        logger.log_action(entry(30))  # the agent goes on logging
        self.assertEqual(len(self.in_file()), 1)

    def test_vague_requests_are_refused(self):
        for body in ({}, {"everything": False}, {"entries": []}, {"status": "Unchanged"}, {"all": True}):
            with self.subTest(body):
                self.assertEqual(self.client.post("/api/logs/remove", json=body).status_code, 422)
        self.assert_left([1, 2, 23, 25, 26])

    def test_the_list_keeps_the_last_200(self):
        app_state.add_actions(entry(100 + i) for i in range(250))
        self.assertEqual(len(app_state.recent_actions), 200)
        self.assertEqual(app_state.recent_actions[-1].timestamp, START + timedelta(hours=349))


class TrimTests(unittest.TestCase):
    """Log files stay small, and a rewrite is rare however long their lines are."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "log.jsonl"

    def test_trim_keeps_whole_newest_lines(self):
        lines = [f'{{"n": {i}, "pad": "{"x" * (i % 700)}"}}' for i in range(3000)]  # 13 to 712 bytes long
        self.path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        logger.trim_jsonl(self.path, max_bytes=500_000, keep_bytes=300_000)
        text = self.path.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.encode()), 300_000)
        self.assertTrue(text.endswith(lines[-1] + "\n"))
        self.assertTrue(text.splitlines()[0] in lines)  # starts at a whole line
        size = self.path.stat().st_size
        logger.trim_jsonl(self.path, max_bytes=500_000, keep_bytes=300_000)  # under the limit: untouched
        self.assertEqual(self.path.stat().st_size, size)

    def test_a_missing_file_is_fine(self):
        logger.trim_jsonl(self.path, max_bytes=10, keep_bytes=5)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
