"""Focus mode's view of the app in front: what counts as a game, OptiMind's own window, an app's
helper processes, and what a give-back puts back. Windows' window functions are faked; nothing
on the PC is changed (the give-back tests use a process of their own)."""
import os
import subprocess
import sys
import unittest
from unittest import mock

import psutil

from app.agent import focus, optimizer
from app.system import monitor

ON_WINDOWS = sys.platform == "win32"
SCREEN = (0, 0, 3440, 1440)


class FakeUser32:
    """GetWindowLongW, MonitorFromWindow, GetWindowRect and GetMonitorInfoW for one window."""

    def __init__(self, rect, caption=False, foreground=77):
        self.rect, self.style, self.foreground = rect, 0x00C00000 if caption else 0x80000000, foreground

    def GetWindowLongW(self, hwnd, index):
        return self.style

    def MonitorFromWindow(self, hwnd, flags):
        return 1

    def GetWindowRect(self, hwnd, rect):
        r = rect._obj
        r.left, r.top, r.right, r.bottom = self.rect
        return 1

    def GetMonitorInfoW(self, monitor_, info):
        info._obj.rcMonitor[:] = SCREEN
        return 1

    def GetForegroundWindow(self):
        return self.foreground


@unittest.skipUnless(ON_WINDOWS, "Windows only")
class FullScreenTests(unittest.TestCase):
    def fills(self, rect, caption=False):
        with mock.patch.object(focus, "_user32", lambda: FakeUser32(rect, caption)):
            return focus._fills_its_screen(77)

    def test_borderless_or_exclusive_full_screen(self):
        self.assertTrue(self.fills(SCREEN))
        self.assertTrue(self.fills((1, 0, 3439, 1440)))  # a pixel lost to rounding on a scaled display

    def test_not_full_screen(self):
        self.assertFalse(self.fills((-8, -8, 3448, 1448), caption=True))  # maximized: it keeps its title bar
        self.assertFalse(self.fills((100, 100, 1943, 1080)))  # a window without a title bar, not covering the screen
        self.assertFalse(focus._fills_its_screen(0))


@unittest.skipUnless(ON_WINDOWS, "Windows only")
class GameTests(unittest.TestCase):
    def test_library_games_and_apps_that_never_are(self):
        self.assertTrue(focus._is_game(focus._Focus(5, "game.exe", r"D:\SteamLibrary\steamapps\common\X\game.exe")))
        chat = focus._Focus(5, "Discord.exe", r"C:\Users\me\AppData\Local\Discord\app-1\Discord.exe", full_screen=True)
        self.assertFalse(focus._is_game(chat))  # e.g. a stream watched full screen

    def test_full_screen_is_its_own_window_not_any_window(self):
        # Real log: Discord and OptiMind's window counted as "a game in focus" while a full-screen game ran behind
        app = focus._Focus(5, "app.exe", r"C:\Apps\app.exe")
        with mock.patch.object(focus, "_user32", lambda: FakeUser32((100, 100, 900, 700))), \
                mock.patch.object(focus, "_window_pid", lambda hwnd: 5), \
                mock.patch.object(focus, "_class_name", lambda hwnd: "AppWindow"):
            self.assertFalse(focus._is_game(app))  # its window doesn't fill the screen
        with mock.patch.object(focus, "_user32", lambda: FakeUser32(SCREEN)), \
                mock.patch.object(focus, "_window_pid", lambda hwnd: 5), \
                mock.patch.object(focus, "_class_name", lambda hwnd: "GameWindow"):
            self.assertTrue(focus._is_game(app))  # it does: a full-screen game that isn't in a library
        with mock.patch.object(focus, "_user32", lambda: FakeUser32((100, 100, 900, 700))), \
                mock.patch.object(focus, "_window_pid", lambda hwnd: 99), \
                mock.patch.object(focus, "_class_name", lambda hwnd: "Chrome_WidgetWin_1"):
            self.assertTrue(focus._is_game(app))  # OptiMind's window over it for a moment: still the game

    def test_work_apps_covering_the_screen_are_not_games(self):
        # Real log: VS Code counted as a game once (maximized over a hidden taskbar, with its own title bar)
        editor = focus._Focus(5, "Code.exe", r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\Code.exe",
                              full_screen=True)
        self.assertFalse(focus._is_game(editor))

    def test_optimind_window_is_never_the_app_in_front(self):
        self.assertTrue(focus._is_own_window(os.getpid()))  # the same program file (from source: python/pythonw)
        self.assertFalse(focus._is_own_window(4))  # Windows' System process


class SnapshotInfoTests(unittest.TestCase):
    def test_the_app_and_whether_it_is_a_game_come_together(self):
        # Real log: "A game is in focus (Discord.exe)": the app and the game state were read apart
        game = focus._Focus(5, "game.exe", r"D:\SteamLibrary\steamapps\common\X\game.exe")
        with mock.patch.object(focus, "_current", game), \
                mock.patch.object(focus, "_thread", mock.Mock(is_alive=lambda: True)):
            self.assertEqual(focus.snapshot_info()[:2], (5, True))
        with mock.patch.object(focus, "_current", game), mock.patch.object(focus, "_thread", None):
            self.assertIsNone(focus.snapshot_info()[0])  # Focus mode isn't watching: the snapshot asks Windows

    def test_a_game_is_noted_once_not_on_every_alt_tab(self):
        # Real log: a game that refuses Above normal was noted on each of 6 returns in 17 minutes
        path = r"D:\SteamLibrary\steamapps\common\X\game.exe"
        with mock.patch.object(focus, "_games_noted", {}), mock.patch.object(focus, "_log", focus.deque(maxlen=50)), \
                mock.patch.object(focus, "_options", lambda: (True, "balanced", False)):
            for boost in ("refused", "refused", "refused", "boosted"):  # back in front three times, then a new result
                with mock.patch.object(focus, "_current", focus._Focus(5, "game.exe", path, boost=boost)):
                    focus.snapshot_info()
            with mock.patch.object(focus, "_current", focus._Focus(6, "other.exe", path, boost="refused")):
                focus.snapshot_info()  # another game
            self.assertEqual([(e.target, e.result) for e in focus.drain_log()], [
                ("game.exe", "Windows refused the priority change (protected game)"),
                ("game.exe", "Above normal priority (checked)"),
                ("other.exe", "Windows refused the priority change (protected game)")])


@unittest.skipUnless(ON_WINDOWS, "Windows only")
class GiveBackTests(unittest.TestCase):
    """Focus mode's boost isn't something the agent held back. Real log: 101 "priority put back"
    give-backs for a browser in two days, most a second or two apart: switching between two of its
    windows run by different processes (a file dialog) gave the boost "back" every time."""

    def setUp(self):
        child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.proc = psutil.Process(child.pid)
        self.name = self.proc.name()
        with optimizer._held_lock:  # what boost() records (Python itself is protected, so it isn't boosted for real)
            self.held = optimizer._hold(self.proc)
            self.held.priority, self.held.boosted = optimizer._NORMAL, True
        self.addCleanup(optimizer._held.pop, child.pid, None)

    def test_the_boost_stays_and_moves_with_the_focus(self):
        self.assertTrue(optimizer.restore_app(self.name).startswith("nothing to put back"))
        self.assertEqual(optimizer.held_back().get(self.name.lower()), ["focus"])
        optimizer.unboost(self.proc.pid)  # leaving the app is what ends it
        self.assertNotIn(self.proc.pid, optimizer._held)

    def test_what_was_held_back_is_put_back_and_the_boost_stays(self):
        with optimizer._held_lock:
            self.held.memory = 5  # its memory priority was lowered (5 = Normal, the value to put back)
        self.assertIn("memory priority put back on 1 instance (checked)", optimizer.restore_app(self.name))
        self.assertEqual(optimizer.held_back().get(self.name.lower()), ["focus"])


class AppTreeTests(unittest.TestCase):
    ROWS = [
        {"pid": 10, "ppid": 1, "name": "Host.exe"},
        {"pid": 11, "ppid": 10, "name": "msedgewebview2.exe"},  # its browser engine
        {"pid": 12, "ppid": 11, "name": "msedgewebview2.exe"},  # ...renderer
        {"pid": 13, "ppid": 10, "name": "Editor.exe"},  # an app it started, with a window of its own
        {"pid": 14, "ppid": 13, "name": "Helper.exe"},  # ...and that app's helper
        {"pid": 20, "ppid": 2, "name": "explorer.exe"},
        {"pid": 21, "ppid": 20, "name": "anything.exe"},  # started from the desktop
    ]

    def test_helpers_belong_to_the_app(self):
        with mock.patch("app.system.processes.app_window_pids", lambda: {10, 13, 20}):
            self.assertEqual(monitor.app_tree(self.ROWS, 10), {10, 11, 12})
            self.assertEqual(monitor.app_tree(self.ROWS, 20), {20})  # the desktop's children are apps of their own
        self.assertEqual(monitor.process_tree(self.ROWS, 10), {10, 11, 12, 13, 14})
        self.assertEqual(monitor.app_tree(self.ROWS, None), set())
        self.assertEqual(monitor.app_tree(self.ROWS, 999), {999})  # not in the list (yet)

    def test_a_parent_loop_doesnt_hang(self):
        rows = [{"pid": 1, "ppid": 2, "name": "a.exe"}, {"pid": 2, "ppid": 1, "name": "b.exe"}]
        self.assertEqual(monitor.process_tree(rows, 1), {1, 2})


if __name__ == "__main__":
    unittest.main()
