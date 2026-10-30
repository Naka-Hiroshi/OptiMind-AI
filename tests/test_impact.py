"""What OptiMind changed, in numbers: what it did (from the activity log, with the amounts Windows
reported), how busy the PC was, and games measured with Focus mode Off and on, side by side.
All files are made up in a temporary folder."""
import json
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.agent import games, impact
from tests import api_client

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat()  # the log's own format: UTC


def entry(action, hours_ago=1, applied=True, result="", target="app.exe"):
    return {"timestamp": at(hours_ago), "action": action, "target": target, "reason": "r", "applied": applied,
            "result": result}


def session(game, mode, fps, low, seconds, minutes=10, hours_ago=2):
    return {"game": game, "exe": "g.exe", "started": (NOW - timedelta(hours=hours_ago)).isoformat(), "minutes": minutes,
            "focus_mode": mode,
            "frames": {"frames": round(fps * seconds), "fps": fps, "low_1_fps": low, "seconds": seconds}
            if fps else None,
            "frames_by_focus_mode": {
                mode: {"frames": round(fps * seconds), "fps": fps, "low_1_fps": low, "seconds": seconds, "captures": 1}
            } if fps and mode else {}}


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="optimind-impact-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log, self.metrics, self.sessions = (self.tmp / n for n in ("log.jsonl", "metrics.jsonl", "sessions.jsonl"))
        for p in (mock.patch.object(impact, "ACTION_LOG_PATH", self.log),
                  mock.patch.object(impact, "METRICS_HISTORY_PATH", self.metrics),
                  mock.patch.object(games, "SESSIONS_PATH", self.sessions)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, path, rows):
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_what_it_did_with_windows_numbers(self):
        self.write(self.log, [
            entry("trim_working_set", result="freed 200 MB of RAM from 1 instance(s) (checked)"),
            entry("trim_working_set", result="freed 1,054 MB of RAM from 3 instance(s) (checked)"),
            entry("trim_working_set", applied=False, result="freed 999 MB"),  # not done: not counted
            entry("trim_working_set", hours_ago=24 * 9, result="freed 500 MB"),  # older than the 7 days
            entry("combine_memory", result="merged 271 MB of identical memory pages into shared copies (checked)"),
            entry("purge_standby_list", result="standby cache purged"),
            entry("lower_priority", target="steam.exe"), entry("efficiency_mode_on", target="steam.exe"),
            entry("lower_io_priority", target="backup.exe"),
            entry("confine_cores"), entry("lower_gpu_priority"), entry("lower_gpu_priority"),
            entry("switch_power_plan"),
        ])
        out = impact.summary(7, now=NOW)
        self.assertEqual(out["memory"], {"freed_mb": 1254, "trims": 2, "merged_mb": 271, "merges": 1, "cache_purges": 1})
        self.assertEqual(out["held_back"], {"times": 3, "apps": [{"name": "steam.exe", "times": 2},
                                                                 {"name": "backup.exe", "times": 1}]})
        self.assertEqual(out["games"], {"one_core": 1, "gpu_priority": 2})
        self.assertEqual(out["power_plan_switches"], 1)

    def test_how_busy_the_pc_was(self):
        rows = [{"t": (NOW - timedelta(seconds=5 * i)).isoformat(), "cpu": 95 if i < 3 else 5} for i in range(100)]
        self.write(self.metrics, list(reversed(rows)))
        load = impact.summary(7, now=NOW)["load"]
        self.assertEqual((load["cpu_median"], load["busy_share"]), (5.0, 0.03))
        self.assertAlmostEqual(load["hours"], 0.1, places=1)
        self.write(self.metrics, rows[:5])
        self.assertIsNone(impact.summary(7, now=NOW)["load"])  # too little to say

    def test_games_side_by_side(self):
        self.write(self.sessions, [
            session("Game A", "off", 60, 40, 100), session("Game A", "off", 50, 30, 100),
            session("Game A", "maximum", 66, 45, 200),
            session("Game B", "maximum", 90, 70, 100),  # never measured with Focus mode Off: no comparison
            session("Game C", None, 80, 60, 100), session("Game C", "off", 75, 50, 100),  # an old session: no mode
            session("Game D", "off", None, None, None),  # not measured
        ])
        out = impact.summary(7, now=NOW)
        self.assertEqual([g["game"] for g in out["compare"]], ["Game A"])
        a = out["compare"][0]
        self.assertEqual(a["modes"]["off"], {"sessions": 2, "captures": 2, "minutes": 20, "fps": 55.0, "low_1_fps": 35.0})
        self.assertEqual(a["change"]["maximum"], {"fps": 20.0, "low_1_fps": 28.6})
        self.assertFalse(a["repeatable"])
        self.assertEqual(out["played"], {"sessions": 7, "minutes": 70, "measured": 6})

    def test_nothing_yet(self):
        out = impact.summary(7, now=NOW)
        self.assertEqual((out["memory"]["trims"], out["held_back"]["times"], out["compare"], out["load"]), (0, 0, [], None))

    def test_a_session_notes_its_focus_mode(self):
        s = games._Session(1, "g.exe", None, "Game", 0, 0, 0)
        s.samples, s.play_seconds = 32, 64.0
        s.stats["ram"].add(50.0)
        s.modes = Counter({"maximum": 30, "off": 2})  # switched for a moment: the mode used most counts
        s.captures = [
            {"frames": 100, "seconds": 2.0, "fps": 50.0, "low_1_fps": 40.0, "stutter_share": 0.0,
             "shares": {}, "cap": None, "gpu_busy": None, "latency_ms": None, "focus_mode": "off"},
            {"frames": 300, "seconds": 3.0, "fps": 100.0, "low_1_fps": 80.0, "stutter_share": 0.0,
             "shares": {}, "cap": None, "gpu_busy": None, "latency_ms": None, "focus_mode": "maximum"},
            {"frames": 300, "seconds": 3.0, "fps": 100.0, "low_1_fps": 80.0, "stutter_share": 0.0,
             "shares": {}, "cap": None, "gpu_busy": None, "latency_ms": None, "focus_mode": None},
        ]
        with mock.patch.object(games, "_match_library", return_value=None), \
                mock.patch.object(games.hardware, "drive_kind", return_value=None):
            summary = games.summarize(s, 60, facts={})
        self.assertEqual(summary["focus_mode"], "maximum")
        self.assertEqual(summary["frames_by_focus_mode"]["off"]["captures"], 1)
        self.assertEqual(summary["frames_by_focus_mode"]["maximum"]["fps"], 100.0)
        self.assertNotIn("focus_mode", summary["frames_by_focus_mode"])

    def test_advice_without_memory_readings(self):
        # A summary can hold max_percent None (no memory sample): advice still comes, without "memory full"
        advice = games.advise({"memory": {"max_percent": None}, "frames": None, "gpu": None}, {})
        self.assertNotIn("ram_full", [a["id"] for a in advice])

    def test_the_api(self):
        from app.main import app

        with mock.patch.object(impact, "summary", return_value={"days": 7}) as summary:
            self.assertEqual(api_client(app).get("/api/impact?days=500").json(), {"days": 7})
        summary.assert_called_once_with(90)  # at most 90 days


if __name__ == "__main__":
    unittest.main()
