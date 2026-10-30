"""The desktop app around the agent: the Settings page (its file, encrypted keys, API),
start with Windows, one OptiMind at a time, and the window's helpers (always dark, from 2.0).
Nothing here changes your PC: the settings file is a temporary one (tests/__init__.py),
no scheduled task is created, and the AI providers are faked."""
import codecs
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from app import __version__, i18n
from app.config import SETTINGS_FILE, settings
from app.desktop import APP_NAME, autostart, instance, shell, tray, window
from app.main import app
from app.models import SettingsUpdate
from app.service import preferences
from app.system import dpapi
from tests import api_client

ON_WINDOWS = sys.platform == "win32"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
INSTALLED = r"C:\Program Files\OptiMind AI\OptiMind.exe"


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.env"

    def tearDown(self):
        self.dir.cleanup()

    def test_new_file(self):
        preferences.write_values(self.path, {"OPTIMIND_AI_PROVIDERS": "groq,local"})
        self.assertEqual(self.path.read_bytes(), b"OPTIMIND_AI_PROVIDERS=groq,local\r\n")

    def test_lines_change_in_place_and_the_rest_is_kept(self):
        original = ("# My notes\r\nGEMINI_API_KEY=old\r\n\r\n# Agent\r\nOPTIMIND_FAST_LOOP_SECONDS=2\r\n"
                    "export GEMINI_API_KEY=older-copy\r\nOPTIMIND_API_PORT=8765")
        self.path.write_bytes(codecs.BOM_UTF8 + original.encode("utf-8"))
        preferences.write_values(self.path, {"GEMINI_API_KEY": "new", "OPTIMIND_GROQ_MODEL": "m-1"})
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8))  # encoding mark kept
        lines = raw[3:].decode("utf-8").split("\r\n")
        self.assertEqual(lines[:6], ["# My notes", "GEMINI_API_KEY=new", "", "# Agent", "OPTIMIND_FAST_LOOP_SECONDS=2",
                                     "GEMINI_API_KEY=new"])  # every copy of the key
        self.assertEqual(lines[6:], ["OPTIMIND_API_PORT=8765", "", "OPTIMIND_GROQ_MODEL=m-1", ""])

    def test_unix_line_endings_are_kept(self):
        self.path.write_text("A=1\nB=2\n", encoding="utf-8", newline="")
        preferences.write_values(self.path, {"B": "3"})
        self.assertEqual(self.path.read_bytes(), b"A=1\nB=3\n")


class SecretsTests(unittest.TestCase):
    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_round_trip(self):
        sealed = dpapi.protect("AIza-test-key")
        self.assertTrue(sealed.startswith(dpapi.PREFIX))
        self.assertNotIn("AIza", sealed)
        self.assertEqual(dpapi.unprotect(sealed), "AIza-test-key")

    def test_plain_and_unreadable_values(self):
        self.assertEqual(dpapi.unprotect("typed-by-hand"), "typed-by-hand")
        self.assertEqual(dpapi.unprotect(""), "")
        self.assertEqual(dpapi.unprotect(dpapi.PREFIX + "not base64!"), "")
        if ON_WINDOWS:
            self.assertEqual(dpapi.unprotect(dpapi.PREFIX + "AAAA"), "")  # another account's (or corrupt) blob


class SettingsApiTests(unittest.TestCase):
    """The Settings page's API, saving to the temporary settings file."""

    @classmethod
    def setUpClass(cls):
        cls.client = api_client(app)
        cls.saved = {f: getattr(settings, f) for f in ("optimind_ai_providers", "gemini_api_key", "groq_api_key",
                                                         "optimind_gemini_model", "optimind_ai_loop_seconds",
                                                         "optimind_run_as_admin", "optimind_measure_games")}

    def tearDown(self):
        for field, value in self.saved.items():
            setattr(settings, field, value)
        SETTINGS_FILE.unlink(missing_ok=True)

    def test_keys_are_never_sent_back(self):
        settings.gemini_api_key = "secret-value"
        body = self.client.get("/api/settings").json()
        self.assertIs(body["gemini_api_key_set"], True)
        self.assertNotIn("secret-value", str(body))
        self.assertEqual(Path(body["file"]), SETTINGS_FILE)

    def test_save(self):
        r = self.client.put("/api/settings", json={"ai_providers": ["groq", "gemini", "groq"], "gemini_api_key": "k-123",
                                                   "gemini_model": "gemini-x", "ai_review_minutes": 30,
                                                   "run_as_admin": False})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["ai_providers"], ["groq", "gemini"])
        self.assertEqual((body["gemini_model"], body["ai_review_minutes"], body["run_as_admin"]), ("gemini-x", 30, False))
        # applied to the running agent right away
        self.assertEqual(settings.optimind_ai_providers, "groq,gemini,local")
        self.assertEqual((settings.gemini_api_key, settings.optimind_ai_loop_seconds), ("k-123", 1800.0))
        text = SETTINGS_FILE.read_text(encoding="utf-8")
        self.assertIn("OPTIMIND_AI_PROVIDERS=groq,gemini,local", text)
        self.assertIn("OPTIMIND_AI_LOOP_SECONDS=1800", text)
        self.assertIn("OPTIMIND_RUN_AS_ADMIN=false", text)
        self.assertNotIn("k-123", text if ON_WINDOWS else "")  # stored encrypted on Windows
        # removing a key
        self.client.put("/api/settings", json={"gemini_api_key": ""})
        self.assertEqual(settings.gemini_api_key, "")
        self.assertIn(b"GEMINI_API_KEY=\r\n", SETTINGS_FILE.read_bytes())

    def test_measuring_games_can_be_switched_off(self):
        body = self.client.put("/api/settings", json={"measure_games": False}).json()
        self.assertIs(body["measure_games"], False)
        self.assertIs(settings.optimind_measure_games, False)  # the running agent stops measuring right away
        self.assertIn("OPTIMIND_MEASURE_GAMES=false", SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertIs(self.client.get("/api/games").json()["status"]["measure_games"], False)

    def test_bad_values_are_refused(self):
        for body in ({"ai_providers": ["openai"]}, {"gemini_api_key": "two words"}, {"ollama_url": "file:///c:/x"},
                     {"ai_review_minutes": 1}, {"gemini_model": ""}, {"optimind_api_port": 1}, {"measure_games": "maybe"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/api/settings", json=body).status_code, 422)
        self.assertFalse(SETTINGS_FILE.exists())

    def test_provider_check(self):
        settings.groq_api_key = ""
        r = self.client.post("/api/settings/test-ai/groq").json()
        self.assertEqual((r["ok"], r["result"]), (False, "GROQ_API_KEY is not set"))
        settings.groq_api_key = "k"
        for status, ok in ((200, True), (401, False)):
            with self.subTest(status=status), mock.patch("httpx.post") as post:
                post.return_value = mock.Mock(status_code=status, json=lambda: {"error": {"message": "Invalid API Key"}})
                r = self.client.post("/api/settings/test-ai/groq").json()
                self.assertIs(r["ok"], ok)
                if not ok:
                    self.assertIn("Invalid API Key", r["result"])
                self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer k"})

    def test_preferences_save_only_touches_given_fields(self):
        preferences.save(SettingsUpdate(groq_model="g-2"))
        self.assertEqual(SETTINGS_FILE.read_text(encoding="utf-8").strip(), "OPTIMIND_GROQ_MODEL=g-2")


class AppApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = api_client(app)

    def test_app_info(self):
        info = self.client.get("/api/app").json()
        self.assertEqual(info["version"], __version__)
        self.assertIs(info["desktop"], False)  # the tests run without the window and tray
        self.assertEqual(set(info["folders"]), {"data", "logs", "settings", "program"})
        self.assertNotIn("appearance", info)  # always dark from 2.0: Windows' colors aren't read
        self.assertIn(info["autostart"], (True, False, None))

    def test_window_actions_need_the_desktop_app(self):
        for path in ("/api/app/window", "/api/app/quit", "/api/app/restart-as-admin"):
            with self.subTest(path):
                self.assertEqual(self.client.post(path).status_code, 409)

    def test_folder_names_are_checked(self):
        self.assertEqual(self.client.post("/api/app/open-folder/windows").status_code, 422)


class AutostartTests(unittest.TestCase):
    def test_task_definition(self):
        xml = autostart.task_xml("S-1-5-21-1-2-3-1001", r"C:\Program Files\OptiMind AI\OptiMind.exe", "--background")
        task = ET.fromstring(xml.split("?>", 1)[1])
        find = lambda path: task.find(path, TASK_NS).text  # noqa: E731
        self.assertEqual(find("t:Principals/t:Principal/t:RunLevel"), "HighestAvailable")
        self.assertEqual(find("t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(find("t:Triggers/t:LogonTrigger/t:UserId"), "S-1-5-21-1-2-3-1001")
        self.assertEqual(find("t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertEqual(find("t:Settings/t:ExecutionTimeLimit"), "PT0S")  # never stopped after 3 days
        self.assertEqual(find("t:Actions/t:Exec/t:Command"), r"C:\Program Files\OptiMind AI\OptiMind.exe")
        self.assertEqual(find("t:Actions/t:Exec/t:Arguments"), "--background")

    def test_special_characters_are_escaped(self):
        xml = autostart.task_xml("S-1", r"C:\A & B\<x>.exe", '"C:\\p q\\run.py" --background')
        task = ET.fromstring(xml.split("?>", 1)[1])
        self.assertEqual(task.find("t:Actions/t:Exec/t:Command", TASK_NS).text, r"C:\A & B\<x>.exe")

    def test_command_from_source(self):
        program, arguments = autostart.command()
        self.assertIn(Path(program).name.lower(), ("pythonw.exe", "python.exe", "python3", "python"))
        self.assertTrue(arguments.endswith('run.py" --background'))

    def test_command_installed(self):
        with mock.patch.object(autostart, "FROZEN", True), mock.patch.object(autostart, "PROGRAM", INSTALLED):
            self.assertEqual(autostart.command(), (INSTALLED, "--background"))  # the program, not sys.executable


class ProgramTests(unittest.TestCase):
    """The installed app starts itself (its window, start with Windows, the prompt for administrator
    rights) by config.PROGRAM: Nuitka's sys.executable names a python.exe that doesn't exist."""

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_program_file(self):
        from app import config

        with mock.patch.object(sys, "executable", r"C:\nowhere\python.exe"):
            path = config._program_file()
        self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(path, os.path.realpath(path))  # full names, not 8.3 short ones

    def test_window_command_installed(self):
        with mock.patch.object(shell, "FROZEN", True), mock.patch.object(shell, "PROGRAM", INSTALLED):
            self.assertEqual(shell.window_command("settings"), [INSTALLED, "--window", "--page=settings"])

    def test_restart_as_admin_installed(self):
        from app.system import admin_rights

        with mock.patch.object(admin_rights, "FROZEN", True), mock.patch.object(admin_rights, "PROGRAM", INSTALLED), \
                mock.patch.object(admin_rights, "ctypes") as fake:  # no real prompt
            fake.WinDLL.return_value.ShellExecuteW.return_value = 42
            self.assertTrue(admin_rights.restart_as_admin(["--background"]))
        args = fake.WinDLL.return_value.ShellExecuteW.call_args.args
        self.assertEqual(args[1:4], ("runas", INSTALLED, f"--background {admin_rights.RELAUNCHED_FLAG}"))


class InstanceTests(unittest.TestCase):
    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_one_at_a_time(self):
        with mock.patch.object(instance, "MUTEX_NAME", f"OptiMindAI.Test.{id(self)}"):
            self.assertFalse(instance.is_running())
            first = instance.claim()
            self.assertTrue(first)
            self.assertTrue(instance.is_running())
            self.assertIsNone(instance.claim())  # a second OptiMind backs off
            instance._kernel32().CloseHandle(first)
            self.assertFalse(instance.is_running())

    def test_nothing_running_to_quit(self):
        with mock.patch.object(instance, "is_running", return_value=False), mock.patch.object(instance, "ask") as ask:
            self.assertTrue(instance.quit_running())
            ask.assert_not_called()


class WindowHelperTests(unittest.TestCase):
    def test_window_command_from_source(self):
        command = shell.window_command("settings")
        self.assertEqual(command[-2:], ["--window", "--page=settings"])
        self.assertEqual(Path(command[1]).name, "run.py")

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_window_starts_with_normal_rights(self):
        # As administrator, the window gets Explorer's normal rights; without them, there's nothing
        # to lower and Windows refuses the call (OptiMind then starts it the usual way).
        from app.system.admin_rights import is_elevated

        if is_elevated():
            child = shell._StandardRightsProcess(["cmd.exe", "/c", "exit 7"])
            self.assertEqual(child.wait(), 7)
        else:
            with self.assertRaises(OSError):
                shell._StandardRightsProcess(["cmd.exe", "/c", "exit 7"])
        with mock.patch.object(shell.admin_rights, "is_elevated", return_value=False), \
                mock.patch("subprocess.Popen") as popen:
            shell._start_window_process(["OptiMind.exe", "--window"])
            popen.assert_called_once()

    def test_title_bar_colors(self):
        self.assertEqual(window._colorref("#202020"), 0x202020)
        self.assertEqual(window._colorref("#0078d4"), 0xD47800)  # COLORREF is blue-green-red
        self.assertEqual(window._colorref(window.BACKGROUND), 0)  # the title bar is the page's black

    def test_only_websites_open_outside(self):
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(window.open_outside("file:///C:/Windows/System32/cmd.exe"))
            self.assertFalse(window.open_outside("javascript:alert(1)"))
            popen.assert_not_called()
            self.assertTrue(window.open_outside("https://ollama.com"))
            popen.assert_called_once_with(["explorer.exe", "https://ollama.com"])
            # a game's page in Steam (Games page), but no other Steam command
            self.assertTrue(window.open_outside("steam://nav/games/details/553850"))
            for command in ("steam://run/553850", "steam://install/1", "steam://uninstall/1",
                            "steam://nav/games/details/553850/../../run", "steam://nav/games/details/1 --x"):
                with self.subTest(command):
                    self.assertFalse(window.open_outside(command))
            self.assertEqual(popen.call_count, 2)

    def test_starting_page(self):
        page = window._status_page("Starting OptiMind…", "The agent is getting ready.", True)
        self.assertIn("background:#000000", page)  # the page's black, no white flash
        self.assertIn("Starting OptiMind…", page)
        self.addCleanup(setattr, settings, "optimind_language", settings.optimind_language)
        settings.optimind_language = "pt-BR"
        page = window._status_page(i18n.tx("Starting OptiMind…"), "<b>&", True)
        self.assertIn('<html lang="pt-BR">', page)
        self.assertIn("<h1>Iniciando o OptiMind…</h1><p>&lt;b&gt;&amp;</p>", page)  # in its language, escaped


class TrayTests(unittest.TestCase):
    def test_the_menu_follows_the_language(self):
        import pystray

        class FakeIcon:  # nothing shown: the menu and the tooltip only
            def __init__(self, name, image, title, menu):
                self.title, self.menu, self.updates = title, menu, 0

            def update_menu(self):
                self.updates += 1

            def run(self):
                pass

        self.addCleanup(setattr, settings, "optimind_language", settings.optimind_language)
        self.addCleanup(i18n._listeners.clear)
        settings.optimind_language = "en"
        patches = mock.patch.object(pystray, "Icon", FakeIcon), mock.patch.object(tray, "is_elevated", return_value=True)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        icon = tray.start(shell.Shell())
        labels = lambda: [i.text for i in icon.menu.items if i is not pystray.Menu.SEPARATOR]  # noqa: E731
        self.assertEqual(labels(), ["Open OptiMind", "Focus mode", "Quit OptiMind"])
        self.assertEqual(icon.title, f"{APP_NAME} {__version__} (administrator)")
        settings.optimind_language = "pt-BR"  # chosen in Settings: the menu is made again in it
        i18n.changed()
        self.assertEqual(labels(), ["Abrir o OptiMind", "Modo de foco", "Sair do OptiMind"])
        self.assertEqual((icon.title, icon.updates), (f"{APP_NAME} {__version__} (administrador)", 1))
        focus = [i.text for i in icon.menu.items[1].submenu.items]
        self.assertEqual(focus, ["Desativado", "Equilibrado", "Máximo (jogos: segundo plano em um núcleo)"])


if __name__ == "__main__":
    unittest.main()
