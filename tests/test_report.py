"""The report to send for help: what's in it, and that nothing personal is (the user name, the
PC's name, e-mail addresses, API keys, game names however they're written, the AI's notes).
Everything is made up in temporary folders; the real Desktop, logs and data are never touched."""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.tools import report
from tests import api_client

USER, PC, GAME, GAME_EXE = "testuser42", "DESKTOP-TEST42", "Secret Game Title", "secretgame.exe"
KEY, GROQ_KEY = "AIza" + "x" * 35, "gsk_" + "y" * 50
DETAILS = {"static": {"os_name": "Windows 11 Pro 25H2 (build 26200)", "cpu_name": "CPU X", "gpu_names": ["GPU Y"],
                      "computer_name": PC},
           "cpu_physical_cores": 4, "cpu_logical_cores": 8, "cpu_base_mhz": 3700, "memory": {"total_mb": 16000},
           "uptime_seconds": 7200, "restart_pending": False}


def personal():
    """The made-up person and PC the tests redact."""
    return [mock.patch.dict(os.environ, {"USERNAME": USER, "USERPROFILE": rf"C:\Users\{USER}"}),
            mock.patch.object(report.platform, "node", return_value=PC),
            mock.patch.object(report.settings, "gemini_api_key", KEY),
            mock.patch.object(report.settings, "groq_api_key", "")]


class RedactionTests(unittest.TestCase):
    def setUp(self):
        for p in personal():
            p.start()
            self.addCleanup(p.stop)

    def test_the_person_and_the_pc(self):
        clean = report._Redactor(set())
        self.assertEqual(clean(rf"C:\Users\{USER}\AppData\x.log on {PC}"), r"C:\Users\<user>\AppData\x.log on <pc>")
        self.assertEqual(clean("TESTUSER42 wrote"), "<user> wrote")  # in any case
        self.assertEqual(clean(f"{USER}x stays"), f"{USER}x stays")  # only where it stands on its own

    def test_keys_and_e_mail(self):
        clean = report._Redactor(set())
        self.assertEqual(clean(f"{KEY}, {GROQ_KEY}, dpapi:{'Q' * 40}"), "<API key>, <API key>, <API key>")
        self.assertEqual(clean("write to someone.name+x@example.co.uk"), "write to <e-mail>")

    def test_games(self):
        clean = report._Redactor({GAME, GAME_EXE})
        self.assertEqual(clean(f"A game is in focus ({GAME_EXE}); {GAME.upper()} runs"),
                         "A game is in focus (a game); a game runs")
        self.assertEqual(clean.data({"app": GAME, "list": [USER, 3], USER: True}),
                         {"app": "a game", "list": ["<user>", 3], "<user>": True})

    def test_games_however_they_are_written(self):
        # A library name with a trademark sign, a game's program, and that program as people write it
        clean = report._Redactor({"Space Raiders™ 2", "sr2.exe", "sr2"})
        self.assertEqual(clean("spaceraiders2.exe, Space Raiders 2, SPACE-RAIDERS 2 and SR2 in a note"),
                         "a game.exe, a game, a game and a game in a note")
        self.assertEqual(clean("Raiders 2, sr22 and space raiders 21 stay"), "Raiders 2, sr22 and space raiders 21 stay")

    def test_which_names_are_games(self):
        activity = [
            {"target": GAME_EXE, "reason": "A game is in focus: the agent acts on background apps using the CPU"},
            {"target": "High performance", "reason": "A game is in focus (other.exe) — switching power plan"},
            # Real log before 1.3.2: an app that never is a game, named in a game's place
            {"target": "High performance", "reason": "A game is in focus (Discord.exe) — switching power plan"},
        ]
        # Real library: OptiMind itself, added to Steam as a shortcut, isn't a game to hide
        library = [{"name": GAME}, {"name": "OptiMind AI"}]
        # Real data too: an old session of OptiMind's own window, and a game whose program is Game.exe
        sessions = [{"game": "Session Game", "exe": "sess.exe"}, {"game": "OptiMind AI", "exe": "OptiMind.exe"},
                    {"game": "Game", "exe": "Game.exe"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(report, "DATA_DIR", Path(tmp)), \
                mock.patch("app.system.hardware.installed_games", return_value=library):
            Path(tmp, "game_sessions.jsonl").write_text("".join(json.dumps(s) + "\n" for s in sessions), encoding="utf-8")
            names = report._game_names(activity)
        # Programs count without ".exe" too, except words like "game" that would garble the whole report
        self.assertEqual(names, {GAME_EXE, "secretgame", "other.exe", "other", GAME, "Session Game", "sess.exe", "sess",
                                 "Game.exe"})

    def test_the_hardware_checks_line_about_games_keeps_its_verdict_only(self):
        checks = {"checks": [
            {"id": "games_on_hdd", "status": "tip", "title": "Games on a hard disk", "summary": f"{GAME} is on D:"},
            {"id": "ram_size", "status": "good", "title": "Memory", "summary": "16 GB"}]}
        with mock.patch("app.system.hardware.cached_report", return_value=checks):
            self.assertEqual([c["summary"] for c in report._hardware_checks()], [None, "16 GB"])


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="optimind-report-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        data, logs, self.out = self.tmp / "data", self.tmp / "logs", self.tmp / "desktop"
        for folder in (data, logs):
            folder.mkdir()
        (logs / "optimind.log").write_text(
            f"2026-10-20 10:00:00 INFO [x] Started from C:\\Users\\{USER}\\AppData on {PC}\n"
            f"2026-10-20 10:00:01 INFO [x] key {KEY}, mail someone@example.com\n"
            f"2026-10-20 10:00:02 INFO [x] Game session ended: {GAME}\n", encoding="utf-8")
        (data / "action_log.jsonl").write_text(json.dumps({
            "timestamp": "2026-10-20T10:00:00", "action": "focus_boost", "target": GAME_EXE, "applied": True,
            "reason": "A game is in focus: the agent acts on background apps using the CPU",
            "result": "Above normal priority (checked)"}) + "\n", encoding="utf-8")
        (data / "game_sessions.jsonl").write_text(json.dumps({"game": GAME, "exe": GAME_EXE}) + "\n", encoding="utf-8")
        patches = personal() + [
            mock.patch.object(report, "DATA_DIR", data), mock.patch.object(report, "LOG_DIR", logs),
            mock.patch.object(report, "_pc", lambda: {"system": {"Manufacturer": "Maker", "Model": "M1",
                                                                 "PCSystemType": 2}}),
            mock.patch.object(report, "_hardware_checks", lambda: [{"id": "ram_size", "status": "good"}]),
            mock.patch("app.system.sysinfo.live_details", return_value=DETAILS),
            mock.patch("app.system.hardware.drives", return_value=[{"letter": "C:", "kind": "NVMe SSD"}]),
            mock.patch("app.system.hardware.installed_games", return_value=[]),
            mock.patch("app.system.drivers.summary", return_value={"findings": []}),
            mock.patch("app.system.gpu.available", return_value=False),
            mock.patch("app.desktop.autostart.status", return_value=False),
            mock.patch("app.system.capabilities.report", return_value=[{"id": "admin", "status": "on"}]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_zip_and_what_it_leaves_out(self):
        result = report.create(self.out)
        path = Path(result["path"])
        self.assertEqual(path.parent, self.out)
        with zipfile.ZipFile(path) as z:
            texts = {name: z.read(name).decode("utf-8") for name in z.namelist()}
        self.assertEqual(sorted(texts), ["README.txt", "logs/optimind.log", "report.json"])
        self.assertEqual(result["files"], ["README.txt", "report.json", "logs/optimind.log"])
        everything = "\n".join(texts.values()).lower()
        for secret in (USER, PC, KEY, "someone@example.com", GAME, GAME_EXE):
            self.assertNotIn(secret.lower(), everything)
        self.assertIn(r"C:\Users\<user>\AppData on <pc>", texts["logs/optimind.log"])
        data = json.loads(texts["report.json"])
        self.assertTrue({"optimind", "windows", "pc", "processor", "graphics", "drives", "activity"} <= set(data))
        self.assertEqual(data["activity"][0]["target"], "a game")
        self.assertEqual(data["processor"], {"name": "CPU X", "cores": 4, "threads": 8, "base_mhz": 3700})
        self.assertEqual(data["pc"]["system"]["PCSystemType"], 2)  # a laptop
        self.assertEqual(data["works_here"], [{"id": "admin", "status": "on"}])  # what works on this PC
        self.assertTrue(data["optimind"]["settings"]["gemini_api_key_set"])  # only whether a key is set
        self.assertEqual(report.last(), path)

    def test_the_ais_notes_stay_out(self):
        from app.service.state import app_state

        # The AI's own words can name a game in a way no list knows ("SRX"), and its list of the apps it
        # protects can hold a game's program written like the library's name without its trademark sign
        policy = app_state.current_policy.model_copy(update={
            "notes": "Protected SRX, the game in front.", "process_denylist_extra": ["spaceraiders2.exe", "Code.exe"]})
        with (self.tmp / "logs" / "optimind.log").open("a", encoding="utf-8") as log:
            log.write("2026-10-20 10:00:03,000 INFO [optimind.orchestrator] Policy updated by gemini: Protected SRX,\n"
                      "the game in front.\n2026-10-20 10:00:04,000 INFO [x] The next line\n")
        with mock.patch.object(app_state, "current_policy", policy), \
                mock.patch("app.system.hardware.installed_games", return_value=[{"name": "Space Raiders™ 2"}]):
            path = Path(report.create(self.out)["path"])
        with zipfile.ZipFile(path) as z:
            log, data = z.read("logs/optimind.log").decode("utf-8"), json.loads(z.read("report.json"))
        self.assertNotIn("SRX", log + json.dumps(data))
        self.assertIn("Policy updated by gemini (the AI's note is left out of the report)\n"
                      "2026-10-20 10:00:04,000 INFO [x] The next line", log)
        self.assertEqual(data["policy"]["notes"], "(the AI's note is left out of the report)")
        self.assertEqual(data["policy"]["process_denylist_extra"], ["a game.exe", "Code.exe"])

    def test_a_part_that_fails_still_leaves_a_report(self):
        with mock.patch("app.system.hardware.drives", side_effect=RuntimeError("no drives")):
            path = Path(report.create(self.out)["path"])
        with zipfile.ZipFile(path) as z:
            data = json.loads(z.read("report.json"))
        self.assertEqual(data["drives"], {"error": "RuntimeError: no drives"})


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = api_client(app)

    def test_create(self):
        made = {"path": "x.zip", "name": "x.zip", "folder": "f", "size": 1, "files": []}
        with mock.patch.object(report, "create", return_value=made):
            self.assertEqual(self.client.post("/api/app/report").json(), made)
        with mock.patch.object(report, "create", side_effect=PermissionError(13, "Access is denied")):
            r = self.client.post("/api/app/report")
        self.assertEqual((r.status_code, r.json()["detail"]), (500, "The report couldn't be saved (Access is denied)."))

    def test_show_only_the_report_made_last(self):
        with mock.patch.object(report, "_last", None):
            self.assertEqual(self.client.post("/api/app/report/show").status_code, 404)
        with tempfile.TemporaryDirectory() as tmp:
            made = Path(tmp, "OptiMind report.zip")
            made.write_bytes(b"zip")
            with mock.patch.object(report, "_last", made), mock.patch("app.api.app_routes.subprocess.Popen") as popen:
                self.assertEqual(self.client.post("/api/app/report/show").status_code, 200)
        self.assertEqual(popen.call_args.args[0], f'explorer.exe /select,"{made}"')


if __name__ == "__main__":
    unittest.main()
