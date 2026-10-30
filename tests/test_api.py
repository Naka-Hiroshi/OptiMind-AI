"""The local API end to end, with OptiMind's loops running in test mode (nothing on the PC
changes; files go to a temporary folder, see tests/__init__.py)."""
import sys
import time
import unittest

from app import __version__
from app.agent import optimizer
from app.ai.prompts import POLICY_JSON_SCHEMA
from app.main import app
from app.models import ActionType
from app.tools import tweaks
from tests import api_client

ON_WINDOWS = sys.platform == "win32"


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = api_client(app)
        cls.client.__enter__()  # starts the loops (lifespan)
        for _ in range(60):
            cls.status = cls.client.get("/api/status").json()
            if cls.status["snapshot"]:
                break
            time.sleep(0.25)

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_status(self):
        st = self.status
        self.assertEqual(st["version"], __version__)
        self.assertIs(st["dry_run"], True)  # test mode
        self.assertTrue(st["snapshot"], "no snapshot after 15 s")
        for key in ("foreground_game", "recent_apps", "audible_apps", "compressed_data_mb", "compression_ratio"):
            self.assertIn(key, st["snapshot"])
        f = st["focus"]
        self.assertEqual((f["enabled"], f["mode"]), (True, "balanced"))
        if f["app"]:
            self.assertEqual(f["boost"], "simulation")  # test mode: the app in front isn't really boosted

    def test_read_only_endpoints(self):
        for path, kind in (("/api/system", dict), ("/api/processes", dict), ("/api/startup-items", list),
                           ("/api/logs", list), ("/api/history?minutes=1", list), ("/api/policy", dict),
                           ("/api/policy/history", list), ("/api/focus", dict)):
            with self.subTest(path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertIsInstance(r.json(), kind)

    def test_focus_mode_switch(self):
        r = self.client.post("/api/focus", json={"enabled": True, "mode": "maximum"}).json()
        self.assertEqual((r["enabled"], r["mode"]), (True, "maximum"))
        self.assertEqual(self.client.get("/api/policy").json()["focus_mode"], "maximum")
        r = self.client.post("/api/focus", json={"enabled": False, "mode": "maximum"}).json()
        self.assertEqual((r["enabled"], r["mode"], r["focus"]["boost"]), (False, "maximum", "off"))
        self.assertEqual(self.client.post("/api/focus", json={"enabled": True, "mode": "turbo"}).status_code, 422)
        self.client.post("/api/focus", json={"enabled": True, "mode": "balanced"})
        self.assertIs(self.client.get("/api/policy").json()["enable_focus_mode"], True)
        self.assertNotIn("focus_mode", POLICY_JSON_SCHEMA["properties"])  # the AI can't change it

    def test_simulation_switch_is_gone(self):
        self.assertEqual(self.client.post("/api/policy/dry-run/true").status_code, 404)

    def test_only_this_pc_may_use_the_api(self):
        body = {"enabled": True, "mode": "balanced"}
        refused = [("another site", {"origin": "https://evil.example"}), ("a sandboxed page", {"origin": "null"}),
                   ("DNS rebinding", {"host": "evil.example:8765"})]
        for label, headers in refused:
            with self.subTest(label):
                self.assertEqual(self.client.post("/api/focus", json=body, headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/focus", json=body, headers={"origin": "http://127.0.0.1:8765"}).status_code, 200)
        self.assertEqual(self.client.post("/api/focus", json=body).status_code, 200)  # tools without an Origin

    def test_dashboard(self):
        r = self.client.get("/")
        self.assertEqual(r.headers["cache-control"], "no-store")  # the window never shows an old copy after an update
        for marker in ('class="pane"', 'id="focus-modes"', 'id="hardware-list"', 'id="tweak-list"', 'id="page-settings"',
                       'id="dialog-host"', 'id="log-clear"', 'id="page-games"', 'id="measure-btn"', 'data-tab="games"',
                       'data-tab="drivers"', 'id="page-drivers"', 'id="drv-findings"', 'id="drv-backup"'):
            self.assertIn(marker, r.text)
        for action in ActionType:  # every activity entry has a label
            self.assertRegex(r.text, rf"\b{action.value}: tx\(\"")
        self.assertNotIn("Simulation mode", r.text)
        self.assertNotIn("confirm(", r.text)  # Windows-style dialogs, not the browser's
        self.assertIn(self.client.get("/favicon.ico").headers["content-type"], ("image/x-icon", "image/vnd.microsoft.icon"))

    def test_games(self):
        data = self.client.get("/api/games").json()
        self.assertEqual(set(data), {"status", "sessions", "library"})
        for key in ("presentmon", "elevated", "measure_games", "gpu", "current", "measuring"):
            self.assertIn(key, data["status"])
        self.assertEqual(set(data["library"]), {"games", "move_first", "best_ssd", "reading"})
        r = self.client.post("/api/games/measure")
        self.assertEqual(r.status_code, 409)  # no game measured here (none running, or no admin rights in tests)
        self.assertIn("detail", r.json())

    def test_system_has_cpu_speed_and_gpu_sensors(self):
        sys_ = self.client.get("/api/system").json()
        self.assertIn("cpu_base_mhz", sys_)
        self.assertIn("gpu_sensors", sys_)  # None without an NVIDIA card

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_tweaks_list(self):
        data = self.client.get("/api/tweaks").json()
        ids = [t["id"] for t in data["tweaks"]]
        self.assertEqual(len(ids), len(set(ids)))
        for t in data["tweaks"]:
            with self.subTest(t["id"]):
                self.assertTrue({"id", "title", "state", "risk", "unavailable", "can_change"} <= set(t))
        by_id = {t["id"]: t for t in data["tweaks"]}
        self.assertEqual(by_id["memory_integrity"]["risk"], "security")
        self.assertNotIn("memory_integrity", tweaks.safe_tweak_ids())  # never applied in bulk

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_hardware_check(self):
        checks = self.client.get("/api/hardware").json()["checks"]
        self.assertTrue(checks)
        rank = {"warn": 0, "tip": 1, "info": 2, "good": 3}
        for c in checks:
            with self.subTest(c.get("id")):
                self.assertTrue({"id", "title", "status", "summary", "detail", "action"} <= set(c))
                self.assertIn(c["status"], rank)
        statuses = [rank[c["status"]] for c in checks]
        self.assertEqual(statuses, sorted(statuses))  # what needs attention first

    def test_nothing_was_held_back(self):
        self.assertEqual(optimizer.held_back(), {})


if __name__ == "__main__":
    unittest.main()
