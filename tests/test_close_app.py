"""Closing an app you picked (Games page, Before you play), as its own X does. None of your apps is
closed: the ones closed here are throwaway processes these tests start, each with an off-screen tool
window of its own (no taskbar button, never activated), and the refusals use made-up process lists."""
import subprocess
import sys
import unittest
from unittest import mock

import psutil
from fastapi.testclient import TestClient

from app.system import processes
from app.tools import close_app
from tests import api_client

ON_WINDOWS = sys.platform == "win32"

# A window that closes on WM_CLOSE ("close"), or only hides, as apps that go to the notification area do ("hide")
CHILD = r"""
import ctypes, sys
from ctypes import wintypes
user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
kernel32.GetModuleHandleW.argtypes, kernel32.GetModuleHandleW.restype = [wintypes.LPCWSTR], wintypes.HMODULE
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
for name in ("GetMessageW",):
    getattr(user32, name).argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
HIDE = sys.argv[1] == "hide"


def proc(hwnd, msg, wparam, lparam):
    if msg == 0x0010 and HIDE:  # WM_CLOSE: hide only
        user32.ShowWindow(hwnd, 0)
        return 0
    if msg == 0x0002:  # WM_DESTROY
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


callback = WNDPROC(proc)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


instance = kernel32.GetModuleHandleW(None)
wc = WNDCLASSW(lpfnWndProc=callback, hInstance=instance, lpszClassName="OptiMindCloseTest")
user32.RegisterClassW(ctypes.byref(wc))
# WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_POPUP, off screen, shown without activating it
hwnd = user32.CreateWindowExW(0x08000080, "OptiMindCloseTest", "OptiMind test", 0x80000000, -32000, -32000, 100, 100,
                              None, None, instance, None)
user32.ShowWindow(hwnd, 4)
print(hwnd or 0, flush=True)
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
"""

ME = psutil.Process().username()


def fake(pid, name, username=ME):
    return mock.Mock(pid=pid, info={"name": name, "username": username})


@unittest.skipUnless(ON_WINDOWS, "Windows only")
class ClosingTests(unittest.TestCase):
    def start(self, how):
        child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", CHILD, how],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 5)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        hwnd = int(child.stdout.readline())
        self.assertTrue(hwnd, "the test window wasn't created")
        return psutil.Process(child.pid), hwnd

    def test_an_app_that_closes(self):
        proc, hwnd = self.start("close")
        out = close_app._close([proc], [hwnd], wait=5)
        self.assertEqual(out["state"], "closed")
        self.assertTrue(out["result"].startswith("closed (1 process ended, about"), out["result"])

    def test_an_app_that_only_hides_in_the_notification_area(self):
        proc, hwnd = self.start("hide")
        out = close_app._close([proc], [hwnd], wait=2)
        self.assertEqual((out["state"], out["freed_mb"]), ("background", 0))
        self.assertIn("right-click its icon there", out["result"])
        self.assertTrue(proc.is_running())  # nothing is ended by force


class RefusalTests(unittest.TestCase):
    def close(self, name, listed=(), windows=None, dry_run=False):
        with mock.patch.object(close_app.psutil, "process_iter", return_value=list(listed)), \
                mock.patch.object(processes, "app_windows", return_value=windows or {}), \
                mock.patch.object(close_app, "_close", side_effect=AssertionError("asked a window to close")), \
                mock.patch.object(close_app, "IS_WINDOWS", True):
            return close_app.close(name, dry_run=dry_run)

    def refused(self, name, *args, **kwargs):
        with self.assertRaises(close_app.CloseError) as caught:
            self.close(name, *args, **kwargs)
        return caught.exception

    def test_windows_security_and_optimind_are_never_closed(self):
        for name in ("explorer.exe", "MsMpEng.exe", "dwm.exe", processes.OWN_NAME):
            with self.subTest(name):
                self.assertEqual(self.refused(name).status, 403)

    def test_what_isnt_closed_and_why(self):
        self.assertEqual(self.refused("Chat.exe").status, 404)  # not running
        other = self.refused("Chat.exe", [fake(10, "Chat.exe", "NT AUTHORITY\\SYSTEM")])
        self.assertIn("another account", str(other))
        no_window = self.refused("Chat.exe", [fake(10, "Chat.exe")], {99: [5]})
        self.assertIn("has no open window", str(no_window))
        self.assertIn("notification area", str(no_window))

    def test_test_mode_only_says_what_it_would_close(self):
        out = self.close("chat.exe", [fake(10, "Chat.exe"), fake(11, "Chat.exe")], {10: [5, 6], 99: [7]}, dry_run=True)
        self.assertEqual(out["result"], "simulated (dry_run): would ask its 2 windows to close")
        self.assertTrue(out["simulated"])  # what the page goes by, in any language


class ApiTests(unittest.TestCase):
    def test_closing_from_the_window_only_and_logged(self):
        from app.main import app
        from app.service.state import app_state

        with mock.patch.object(close_app.psutil, "process_iter", return_value=[fake(10, "Chat.exe")]), \
                mock.patch.object(processes, "app_windows", return_value={10: [5]}), \
                mock.patch.object(close_app, "_close", side_effect=AssertionError("closed in test mode")), \
                mock.patch.object(close_app, "IS_WINDOWS", True):
            r = api_client(app).post("/api/apps/close", json={"name": "Chat.exe"})
            self.assertEqual((r.status_code, r.json()["state"]), (200, "open"))
            entry = app_state.recent_actions[-1]
            self.assertEqual((entry.action.value, entry.target, entry.applied), ("close_app", "Chat.exe", False))
            self.assertTrue(entry.result.startswith("simulated"))  # "Logged only" in the activity
            anyone = TestClient(app, base_url="http://127.0.0.1:8765")  # without the window's secret
            self.assertEqual(anyone.post("/api/apps/close", json={"name": "Chat.exe"}).status_code, 403)
        self.assertEqual(api_client(app).post("/api/apps/close", json={"name": "explorer.exe"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
