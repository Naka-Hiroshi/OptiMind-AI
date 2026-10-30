"""The safety layer and honest results: protected processes are refused before anything
runs, and a change Windows refuses is logged as an error, never as "applied". Nothing here
changes a real process: permission checks only open a handle, and "access denied" comes
from stand-in objects."""
import os
import sys
import unittest
from unittest import mock

import psutil

from app.agent import optimizer, safety
from app.models import ActionType, OptimizationAction

ON_WINDOWS = sys.platform == "win32"


def action(kind, target):
    return OptimizationAction(action=kind, target=target, reason="test", dry_run=False)


class Denied:
    """A process Windows won't let OptiMind change."""
    def __init__(self, pid):
        self.pid = pid

    def nice(self, _=None):
        raise psutil.AccessDenied(self.pid)

    def ionice(self, _=None):
        raise psutil.AccessDenied(self.pid)


class Allowed:
    """A process that accepts the change (in memory only)."""
    def __init__(self, pid):
        self.pid, self._nice = pid, psutil.NORMAL_PRIORITY_CLASS if ON_WINDOWS else 0

    def nice(self, value=None):
        if value is None:
            return self._nice
        self._nice = value

    def create_time(self):
        return 1.0

    def name(self):
        return "busy.exe"


class SafetyLayerTests(unittest.TestCase):
    def test_essential_processes_are_protected(self):
        for name in ("csrss.exe", "MsMpEng.exe", "explorer.exe", "dwm.exe", "ctfmon.exe", "Memory Compression"):
            with self.subTest(name):
                self.assertTrue(safety.is_protected(name))
        self.assertFalse(safety.is_protected("chrome.exe"))
        self.assertTrue(safety.is_protected("Chrome.exe", ["chrome.exe"]))  # the policy's own list

    def test_every_process_action_is_checked(self):
        for kind in (ActionType.LOWER_PRIORITY, ActionType.EFFICIENCY_MODE_ON, ActionType.LOWER_IO_PRIORITY,
                     ActionType.TRIM_WORKING_SET, ActionType.CONFINE_CORES, ActionType.RESTORE_APP):
            with self.subTest(kind.value), self.assertRaises(safety.SafetyViolation):
                safety.validate_action(action(kind, "lsass.exe"))

    def test_the_ais_list_keeps_priority_not_idle_memory(self):
        listed = ["Code.exe"]
        for kind in (ActionType.LOWER_PRIORITY, ActionType.EFFICIENCY_MODE_ON, ActionType.LOWER_IO_PRIORITY,
                     ActionType.CONFINE_CORES, ActionType.LOWER_GPU_PRIORITY):
            with self.subTest(kind.value), self.assertRaises(safety.SafetyViolation):
                safety.validate_action(action(kind, "code.exe"), listed)
        for kind in (ActionType.TRIM_WORKING_SET, ActionType.RESTORE_APP, ActionType.RESTORE_CORES):
            with self.subTest(kind.value):
                safety.validate_action(action(kind, "code.exe"), listed)  # compressing idle memory, giving back
        with self.assertRaises(safety.SafetyViolation):
            safety.validate_action(action(ActionType.TRIM_WORKING_SET, "MsMpEng.exe"), listed)  # the built-in list

    def test_startup_apps_need_your_approval(self):
        with self.assertRaises(safety.SafetyViolation):
            safety.validate_action(action(ActionType.DISABLE_STARTUP_ITEM, "Discord"))
        safety.validate_action(action(ActionType.DISABLE_STARTUP_ITEM, "Some Updater"))  # known safe pattern

    def test_a_protected_process_is_refused_before_anything_runs(self):
        entry = optimizer._apply(action(ActionType.LOWER_PRIORITY, "MsMpEng.exe"), None)
        self.assertFalse(entry.applied)
        self.assertTrue(entry.result.startswith("blocked_by_safety"), entry.result)


@unittest.skipUnless(ON_WINDOWS, "Windows only")
class HonestResultTests(unittest.TestCase):
    def tearDown(self):
        optimizer._held.clear()  # the stand-ins' "changes" were remembered in memory and in the temporary data folder
        optimizer.HELD_BACK_PATH.unlink(missing_ok=True)

    def test_permission_checks(self):
        self.assertIsNone(optimizer.access_problem(os.getpid()))  # its own process
        self.assertIsNotNone(optimizer.access_problem(4))  # System
        defender = next((p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "msmpeng.exe"), None)
        if defender and not optimizer.is_elevated():
            self.assertEqual(optimizer.access_problem(defender), optimizer.OTHER_ACCOUNT)

    def test_nothing_changed_is_logged_as_an_error(self):
        with mock.patch.object(optimizer, "_find_processes_by_name", lambda name: [Denied(4)]):
            entry = optimizer._apply(action(ActionType.LOWER_PRIORITY, "busy.exe"), None)
            self.assertFalse(entry.applied)
            self.assertEqual(entry.result, f"error: not changed, {optimizer.PROTECTED_BY_WINDOWS}")
            entry = optimizer._apply(action(ActionType.LOWER_IO_PRIORITY, "busy.exe"), None)
            self.assertFalse(entry.applied)
            self.assertTrue(entry.result.startswith("error: not changed"), entry.result)

    def test_nothing_found_is_logged_as_unchanged(self):
        # Real log: "not found" trims were logged as applied, and counted as memory compressed
        gone = OptimizationAction(action=ActionType.TRIM_WORKING_SET, target="gone.exe", reason="test", dry_run=False,
                                  pids=[2 ** 31 - 3])  # no such process (Windows' PIDs are multiples of 4)
        entry = optimizer._apply(gone, None)
        self.assertFalse(entry.applied)
        self.assertTrue(entry.result.startswith(optimizer.NOT_FOUND), entry.result)

    def test_a_partial_success_says_what_was_skipped(self):
        result = optimizer._lower_priority([Allowed(os.getpid() + 1), Denied(4)])
        self.assertTrue(result.endswith("; 1 skipped: no permission"), result)

    def test_standby_purge_needs_administrator_rights(self):
        if optimizer.is_elevated():
            self.skipTest("running as administrator")
        entry = optimizer._apply(action(ActionType.PURGE_STANDBY_LIST, "system"), None)
        self.assertFalse(entry.applied)
        self.assertIn("administrator", entry.result)


if __name__ == "__main__":
    unittest.main()
