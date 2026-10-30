"""Disk clean-up's safety rules, on folders of its own (a temporary folder): only the folder it
claims, only old files, never through a link or junction, files in use skipped, empty
subfolders removed but never the folder itself. Nothing outside the temporary folder is touched."""
import contextlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.tools import cleanup

ON_WINDOWS = sys.platform == "win32"
DAY = 24 * 3600


class CleanupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.temp = self.root / "AppData" / "Local" / "Temp"  # what a user's Temp folder looks like
        (self.temp / "sub" / "deeper").mkdir(parents=True)
        for name in ("a.tmp", "sub/b.log", "sub/deeper/c.dat"):
            (self.temp / name).write_text("x" * 100, encoding="utf-8")
        self.outside = self.root / "keep"
        self.outside.mkdir()
        (self.outside / "precious.txt").write_text("mine", encoding="utf-8")
        self.folder = cleanup.Folder(self.temp, r"\appdata\local\temp")

    def files(self, now_offset=3 * DAY, min_age=DAY):
        return sorted(Path(p).relative_to(self.temp).as_posix()
                      for p, _ in cleanup._files(self.folder, min_age, time.time() + now_offset))

    def test_only_the_folder_it_claims(self):
        self.assertTrue(cleanup._valid(self.folder))
        self.assertFalse(cleanup._valid(cleanup.Folder(Path("C:/"), r"\appdata\local\temp")))  # TEMP set to C:\
        self.assertFalse(cleanup._valid(cleanup.Folder(self.outside, r"\appdata\local\temp")))
        self.assertFalse(cleanup._valid(cleanup.Folder(Path("Temp"), r"\temp")))  # not a full path

    def test_only_old_files(self):
        self.assertEqual(self.files(), ["a.tmp", "sub/b.log", "sub/deeper/c.dat"])
        self.assertEqual(self.files(now_offset=0), [])  # just written: may still be in use

    def test_a_pattern_and_no_subfolders(self):
        thumbs = cleanup.Folder(self.temp, r"\appdata\local\temp", recursive=False, pattern="*.tmp")
        found = [Path(p).name for p, _ in cleanup._files(thumbs, 0, time.time() + DAY)]
        self.assertEqual(found, ["a.tmp"])

    def _old_files(self, folder, min_age, now, deadline=None):
        """cleanup._files as if three days had passed: everything written here counts as old."""
        return self._real_files(folder, min_age, now + 3 * DAY, deadline)

    def clean(self, **patches):
        """Cleans this test's folder as a location of its own, three days later."""
        self._real_files = cleanup._files
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(cleanup._BY_ID, {"test": cleanup.Location("test", "Test", "",
                                                                                          lambda: [self.folder])}))
            stack.enter_context(mock.patch.object(cleanup, "_files", self._old_files))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(cleanup, name, value))
            return cleanup.run(["test"])

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_never_through_a_junction(self):
        link = self.temp / "link"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.outside)], capture_output=True)
        if made.returncode != 0:
            self.skipTest("couldn't make a junction here")
        self.addCleanup(lambda: link.exists() and os.rmdir(link))  # the junction only, never what it points to
        self.assertNotIn("link/precious.txt", self.files())
        result = self.clean()
        self.assertTrue((self.outside / "precious.txt").exists())  # nothing deleted through the junction
        self.assertTrue(link.exists())
        self.assertEqual(result["results"][0]["deleted"], 3)

    def test_cleaning_deletes_old_files_and_empty_folders_only(self):
        result = self.clean(_born=lambda info: info.st_mtime - 3 * DAY)  # the subfolders count as old too
        self.assertEqual((result["results"][0]["deleted"], result["freed_bytes"]), (3, 300))
        self.assertEqual(list(self.temp.iterdir()), [])  # the empty subfolders went too...
        self.assertTrue(self.temp.is_dir())  # ...but never the folder itself
        self.assertTrue((self.outside / "precious.txt").exists())

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_a_file_in_use_is_skipped(self):
        held = open(self.temp / "a.tmp", "rb")  # noqa: SIM115 - Windows won't delete an open file
        self.addCleanup(held.close)
        result = self.clean()
        self.assertEqual((result["results"][0]["deleted"], result["results"][0]["skipped_in_use"]), (2, 1))
        self.assertTrue((self.temp / "a.tmp").exists())

    def test_unknown_or_no_locations_are_refused(self):
        with self.assertRaises(cleanup.CleanupError):
            cleanup.run([])
        with self.assertRaises(cleanup.CleanupError):
            cleanup.run(["../../windows"])


if __name__ == "__main__":
    unittest.main()
