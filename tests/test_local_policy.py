"""The Local Group Policy writer (what gpedit.msc edits), on a temporary folder only.
gpupdate never runs (a stand-in answers), and the real policy files are only read."""
import codecs
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.tools import local_policy as lp

DSH = r"SOFTWARE\Policies\Microsoft\Dsh"
CSE, TOOL = lp._REGISTRY_EXTENSION


def u16(s):
    return s.encode("utf-16-le")


TRICKY = [
    lp.Entry(r"Software\Policies\A", "Number", 4, struct.pack("<I", 0x005D003B)),  # data that looks like ';' and ']'
    lp.Entry(r"Software\Policies\A", "Text", 1, u16("a;b]c\0")),
    lp.Entry(r"Software\Policies\A", "**del.Old", 1, u16(" \0")),
    lp.Entry(r"Software\Policies\B", "", 0, b""),  # key only
    lp.Entry(r"Software\Policies\Ünïcode", "Wert", 11, struct.pack("<Q", 2 ** 40)),
]


def _real_files():
    real, out = lp._gpo_dir(), {}
    for rel in ("gpt.ini", r"Machine\Registry.pol", r"User\Registry.pol"):
        try:
            out[rel] = (real / rel).read_bytes()
        except FileNotFoundError:
            out[rel] = None
        except PermissionError:
            out[rel] = "no access"
    return out


class LocalPolicyTestCase(unittest.TestCase):
    """Each test gets its own empty policy folder in place of %SystemRoot%\\System32\\GroupPolicy."""

    @classmethod
    def setUpClass(cls):
        cls.real_before = _real_files()

    @classmethod
    def tearDownClass(cls):
        assert _real_files() == cls.real_before, "the real local Group Policy files changed"

    def setUp(self):
        self.box = Path(tempfile.mkdtemp(prefix="optimind-gpo-"))
        patcher = mock.patch.object(lp, "_gpo_dir", lambda: self.box)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.box, ignore_errors=True)
        self.pol = self.box / "Machine" / "Registry.pol"
        self.ini = self.box / "gpt.ini"

    def put_ini(self, text):
        self.ini.write_bytes(text.encode("ascii"))  # bytes: no newline translation

    def ini_text(self):
        return self.ini.read_bytes().decode("latin-1")


class FileFormatTests(LocalPolicyTestCase):
    def test_stored_byte_for_byte_as_gpedit_stores_it(self):
        expected = (b"PReg\x01\x00\x00\x00" + u16("[") + u16(DSH + "\0") + u16(";") + u16("AllowNewsAndInterests\0")
                    + u16(";") + struct.pack("<I", 4) + u16(";") + struct.pack("<I", 4) + u16(";")
                    + struct.pack("<I", 0) + u16("]"))
        built = lp.build([lp.value_entry(DSH, "AllowNewsAndInterests", lp.REG_DWORD, 0)])
        self.assertEqual(built[:8], struct.pack("<II", 0x67655250, 1))  # 'PReg', version 1
        self.assertEqual(built, expected)

    def test_round_trip(self):
        data = lp.build(TRICKY)
        self.assertEqual(lp.parse(data), TRICKY)  # ';' or ']' in data, deletes, key-only, Unicode
        self.assertEqual(lp.build(lp.parse(data)), data)
        self.assertEqual(lp.parse(b""), [])

    def test_damaged_files_are_refused(self):
        data = lp.build(TRICKY)
        for label, bad in (("wrong header", b"XReg\x01\0\0\0" + data[8:]), ("version 2", b"PReg\x02\0\0\0" + data[8:]),
                           ("cut short", data[:-3]), ("missing ']'", data[:-2]), ("junk after an entry", data + b"\x00")):
            with self.subTest(label), self.assertRaises(lp.PolicyError):
                lp.parse(bad)

    def test_values(self):
        self.assertEqual(lp.value_entry("K", "S", lp.REG_SZ, "hi").data, u16("hi\0"))  # strings end with NUL
        with self.assertRaises(lp.PolicyError):
            lp.value_entry("K", "B", 3, b"x")  # unsupported type


class ReplaceTests(LocalPolicyTestCase):
    def test_creates_the_file_when_there_is_none(self):
        before = lp.replace({(DSH, "AllowNewsAndInterests"): [lp.value_entry(DSH, "AllowNewsAndInterests", 4, 0)]})
        self.assertTrue(self.pol.exists())
        self.assertEqual(before, {(DSH, "AllowNewsAndInterests"): []})
        found = [e.data for e in lp.entries_for(DSH.lower(), "allownewsandinterests")]  # any letter case
        self.assertEqual(found, [struct.pack("<I", 0)])

    def test_only_the_chosen_value_changes_and_can_be_put_back(self):
        other = lp.Entry(r"Software\Policies\Microsoft\Windows\Explorer", "NoAutoplay", 4, struct.pack("<I", 1))
        old_delete = lp.Entry(DSH, "**del.AllowNewsAndInterests", 1, u16(" \0"))
        self.pol.parent.mkdir(parents=True)
        self.pol.write_bytes(lp.build([other, old_delete, TRICKY[0]]))
        original = self.pol.read_bytes()
        before = lp.replace({(DSH, "AllowNewsAndInterests"): [lp.value_entry(DSH, "AllowNewsAndInterests", 4, 0)]})
        self.assertEqual(lp._load(), [other, lp.value_entry(DSH, "AllowNewsAndInterests", 4, 0), TRICKY[0]])
        self.assertEqual(before, {(DSH, "AllowNewsAndInterests"): [old_delete]})  # reports what was there
        lp.replace(before)
        self.assertEqual(self.pol.read_bytes(), original)  # put back byte for byte
        self.assertFalse(any(p.name.endswith(".optimind-tmp") for p in self.pol.parent.iterdir()))

    def test_a_damaged_policy_file_is_never_overwritten(self):
        self.pol.parent.mkdir(parents=True)
        self.pol.write_bytes(b"garbage")
        with self.assertRaises(lp.PolicyError):
            lp.replace({(DSH, "AllowNewsAndInterests"): []})
        self.assertEqual(self.pol.read_bytes(), b"garbage")


class GptIniTests(LocalPolicyTestCase):
    def test_a_new_gpt_ini_looks_like_gpedits(self):
        lp._raise_version()
        self.assertEqual(self.ini.read_bytes(),
                         f"[General]\r\ngPCFunctionalityVersion=2\r\ngPCMachineExtensionNames=[{CSE}{TOOL}]\r\nVersion=1\r\n".encode())
        lp._raise_version()
        self.assertIn("Version=2\r\n", self.ini_text())  # each change raises the computer version

    def test_other_settings_are_kept(self):
        security = "[{827D319E-6EAC-11D2-A4EA-00C04F79F83A}{803E14A0-B4FB-11D0-A0D0-00A0C90F574B}]"
        user_names = "[{35378EAC-683F-11D2-A89A-00C04FBBCFA2}{D02B1F73-3407-48AE-BA88-E8213C6761F1}]"
        self.put_ini(f"[General]\r\ngPCFunctionalityVersion=2\r\ngPCMachineExtensionNames={security}\r\nVersion=65537\r\n"
                     f"gPCUserExtensionNames={user_names}\r\n\r\n[Other]\r\nkeep=me\r\n")
        lp._raise_version()
        text = self.ini_text()
        self.assertIn("Version=65538\r\n", text)  # user version kept, computer version raised
        self.assertIn(f"gPCMachineExtensionNames=[{CSE}{TOOL}]{security}\r\n", text)  # sorted by GUID
        self.assertIn(f"gPCUserExtensionNames={user_names}\r\n", text)
        self.assertIn("[Other]\r\nkeep=me\r\n", text)

    def test_existing_registry_group_and_version_wrap(self):
        self.put_ini(f"[General]\r\ngPCMachineExtensionNames=[{CSE}{{0F6B957D-509E-11D1-A7CC-0000F87571E3}}]\r\nversion=131071\r\n")
        lp._raise_version()
        text = self.ini_text()
        self.assertIn(f"=[{CSE}{{0F6B957D-509E-11D1-A7CC-0000F87571E3}}{TOOL}]", text)
        self.assertIn("Version=65537", text)  # computer version wraps to 1, not 0
        lp._raise_version()
        self.assertEqual(self.ini_text().count(CSE), 1)  # never listed twice

    def test_missing_general_section_and_utf16(self):
        self.put_ini("[Other]\r\nx=1\r\n")
        lp._raise_version()
        text = self.ini_text()
        self.assertTrue(text.startswith("[General]\r\n") and "Version=1" in text and "[Other]\r\nx=1" in text, text)
        self.ini.write_bytes(codecs.BOM_UTF16_LE + "[General]\r\nVersion=5\r\n".encode("utf-16-le"))
        lp._raise_version()
        raw = self.ini.read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF16_LE) and "Version=6" in raw.decode("utf-16"))


class RefreshTests(unittest.TestCase):
    """What OptiMind makes of gpupdate's outcome (a stand-in answers; gpupdate never runs)."""

    def run_with(self, result):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(lp.subprocess, "run", run):
            return lp.refresh(), calls

    def test_success(self):
        problem, calls = self.run_with(subprocess.CompletedProcess([], 0, "Computer Policy update has completed successfully.\n", ""))
        self.assertIsNone(problem)
        cmd, kw = calls[-1]
        self.assertTrue(cmd[0].lower().endswith(r"\gpupdate.exe"))
        self.assertEqual(cmd[1:], ["/target:computer", "/force", "/wait:90"])  # computer only, forced
        self.assertEqual(kw["input"], "N\nN\n")  # answers 'no' to sign-out/restart prompts
        self.assertEqual(kw["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_failures_say_why(self):
        failed = subprocess.CompletedProcess([], 1, "Updating policy...\n", "Computer policy could not be updated successfully.\n")
        self.assertEqual(self.run_with(failed)[0], "gpupdate failed (Computer policy could not be updated successfully.)")
        self.assertIn("no Group Policy", self.run_with(FileNotFoundError())[0])
        self.assertEqual(self.run_with(subprocess.TimeoutExpired("gpupdate", 100))[0],
                         "Windows took too long to apply the policy")


if __name__ == "__main__":
    unittest.main()
