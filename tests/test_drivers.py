"""The Drivers page: reading devices, driver packages, app drivers and Windows' logs, the
findings built from them, the update checks (Windows Update, NVIDIA) and the driver tools.
Everything that would change the PC is tried on stand-ins: no driver is installed, removed
or backed up, and nothing is downloaded."""
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.models import ActionType
from app.system import devices, driver_events, driver_store, drivers, software_drivers
from app.tools import driver_actions, driver_jobs, driver_updates
from tests import api_client

ON_WINDOWS = sys.platform == "win32"
TODAY = date(2026, 10, 19)


def device(name, cls="System", provider="Advanced Micro Devices, Inc", version="1.0.0.1", when="2025-09-09",
           inf="oem3.inf", present=True, problem=0, bus="PCI", service="svc", instance=None, hardware_id=None,
           manufacturer=""):
    instance = instance or f"PCI\\VEN_1022&DEV_{abs(hash(name)) % 9999:04d}\\3&1"
    d = devices.describe(instance, {"name": name, "class": cls, "provider": provider, "driver_version": version,
                                    "driver_date": datetime.fromisoformat(when) if when else None, "inf": inf,
                                    "bus": bus, "service": service, "manufacturer": manufacturer,
                                    "hardware_ids": [hardware_id or instance.rsplit("\\", 1)[0]]}, present, problem)
    d.pop("ids")
    return d


# ------------------------------------------------------------ devices

class DeviceTests(unittest.TestCase):
    def test_problem_codes_have_plain_explanations(self):
        what, fix = devices.problem_text(43)
        self.assertIn("stopped", what)
        self.assertIn("Restart the device", fix)
        what, fix = devices.problem_text(999)
        self.assertIn("999", what)
        self.assertNotIn(22, devices.NOT_PROBLEMS)  # turned off is reported, as information
        self.assertTrue({0, 45, 46} <= devices.NOT_PROBLEMS)

    def test_property_values(self):
        self.assertEqual(devices.decode(0x12, "Realtek\0".encode("utf-16-le")), "Realtek")
        self.assertEqual(devices.decode(0x2012, "A\0B\0\0".encode("utf-16-le")), ["A", "B"])
        self.assertEqual(devices.decode(0x7, (43).to_bytes(4, "little")), 43)
        self.assertIs(devices.decode(0x11, b"\xff"), True)
        when = devices.decode(0x10, (133_000_000_000_000_000).to_bytes(8, "little"))
        self.assertEqual(when.year, 2022)
        self.assertIsNone(devices.decode(0x10, bytes(8)))

    def test_a_device_as_the_page_sees_it(self):
        d = devices.describe("PCI\\VEN_10DE&DEV_1F06\\4&1", {
            "name": "NVIDIA GeForce RTX 2060 SUPER", "hardware_ids": ["PCI\\VEN_10DE&DEV_1F06&SUBSYS_13A310DE"],
            "compatible_ids": ["PCI\\VEN_10DE&DEV_1F06"], "inf": "OEM37.INF", "driver_version": "32.0.16.1742",
            "driver_date": datetime(2026, 9, 30, tzinfo=timezone.utc), "provider": "NVIDIA"}, True, 0)
        self.assertEqual(d["ids"], ["pci\\ven_10de&dev_1f06&subsys_13a310de", "pci\\ven_10de&dev_1f06"])
        self.assertEqual((d["driver"]["inf"], d["driver"]["date"]), ("oem37.inf", "2026-09-30"))
        self.assertEqual(d["bus"], "PCI")
        bare = devices.describe("ROOT\\X\\0", {}, False, 28)
        self.assertIsNone(bare["driver"])
        self.assertEqual(bare["problem"], 0)  # not connected: no problem shown

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_reading_this_pc(self):
        started = time.perf_counter()
        found = devices.read_devices()
        self.assertLess(time.perf_counter() - started, 5)
        self.assertTrue(found)
        self.assertTrue(any(d["present"] for d in found))
        for key in ("id", "name", "class", "present", "problem", "driver", "ids"):
            self.assertIn(key, found[0])


# ------------------------------------------------------------ driver store

_XML = """<?xml version="1.0" encoding="utf-8"?>
<PnpUtil Version="10.0.26300" Command="/enum-drivers /devices /format xml">
  <Driver DriverName="oem10.inf"><OriginalName>amdgpio2.inf</OriginalName><ProviderName>Advanced Micro Devices, Inc</ProviderName>
    <ClassName>System</ClassName><ClassGuid>{4d36e97d-e325-11ce-bfc1-08002be10318}</ClassGuid>
    <DriverVersion>09/29/2019 2.2.0.121</DriverVersion><SignerName>Microsoft Windows Hardware Compatibility Publisher</SignerName>
    <CatalogAttributes><Attribute>Universal</Attribute></CatalogAttributes></Driver>
  <Driver DriverName="oem32.inf"><OriginalName>amdgpio2.inf</OriginalName><ProviderName>Advanced Micro Devices, Inc.</ProviderName>
    <ClassName>System</ClassName><DriverVersion>03/22/2026 2.2.0.137</DriverVersion>
    <Devices><Device InstanceId="ACPI\\AMDI0030\\0"><DeviceDescription>AMD GPIO Controller</DeviceDescription><Status>Started</Status></Device></Devices></Driver>
</PnpUtil>"""


def package(name, original, version, when, used=False, provider="NVIDIA", cls="Display", size=100):
    return {"name": name, "original": original, "provider": provider, "class": cls, "version": version, "date": when,
            "devices": [{"id": "X", "name": "Card", "status": "Started"}] if used else [], "size": size}


class StoreTests(unittest.TestCase):
    def test_versions_and_dates(self):
        self.assertEqual(driver_store.parse_driver_ver("09/30/2026 32.0.16.1742"), ("2026-09-30", "32.0.16.1742"))
        self.assertEqual(driver_store.parse_driver_ver("6/21/2006,10.0.26100.1"), ("2006-06-21", "10.0.26100.1"))
        self.assertEqual(driver_store.parse_driver_ver("garbage")[0], None)
        self.assertEqual(driver_store.version_key("10.010.0714.2016"), driver_store.version_key("10.10.714.2016"))
        self.assertGreater(driver_store.version_key("32.0.16.1742"), driver_store.version_key("32.0.16.1714"))

    def test_pnputil_xml(self):
        pk = driver_store.parse_packages(_XML)
        self.assertEqual([p["name"] for p in pk], ["oem10.inf", "oem32.inf"])
        self.assertEqual((pk[0]["date"], pk[0]["version"], pk[0]["attributes"]), ("2019-09-29", "2.2.0.121", ["Universal"]))
        self.assertEqual(pk[0]["class_guid"], "4d36e97d-e325-11ce-bfc1-08002be10318")
        self.assertEqual(pk[1]["devices"][0]["name"], "AMD GPIO Controller")
        driver_store.mark_old_versions(pk)  # "Inc" and "Inc." are the same maker
        self.assertEqual((pk[0]["old"], pk[0]["rollback"], pk[0]["removable"], pk[1]["in_use"]), (True, True, False, True))

    def test_old_versions_keep_the_one_to_roll_back_to(self):
        pk = [package("oem1.inf", "nv_dispi.inf", "32.0.15.6094", "2024-08-13"),
              package("oem2.inf", "nv_dispi.inf", "32.0.16.1714", "2026-09-22"),
              package("oem3.inf", "nv_dispi.inf", "32.0.16.1742", "2026-09-30", used=True),
              package("oem4.inf", "nv_dispi.inf", "32.0.14.9999", "2023-01-01")]
        driver_store.mark_old_versions(pk)
        by = {p["name"]: p for p in pk}
        self.assertTrue(by["oem2.inf"]["rollback"])
        self.assertFalse(by["oem2.inf"]["removable"])
        self.assertTrue(by["oem1.inf"]["removable"] and by["oem4.inf"]["removable"])
        self.assertFalse(by["oem3.inf"]["old"])

    def test_left_alone(self):
        newer_but_unused = [package("oem18.inf", "nvppc.inf", "1.52.831.832", "2025-08-21", used=True, cls="USB"),
                            package("oem8.inf", "nvppc.inf", "1.52.831.832", "2026-01-20", cls="USB")]
        unplugged = [package("oem5.inf", "logi.inf", "1.0", "2020-01-01", provider="Logitech", cls="HIDClass"),
                     package("oem6.inf", "logi.inf", "2.0", "2026-01-01", provider="Logitech", cls="HIDClass")]
        driver_store.mark_old_versions(newer_but_unused + unplugged)
        self.assertFalse(any(p["old"] for p in newer_but_unused + unplugged))

    def test_who_signed_a_package(self):
        self.assertEqual(driver_store.signing("Microsoft Windows"), "windows")
        self.assertEqual(driver_store.signing("Microsoft Windows Hardware Compatibility Publisher"), "microsoft")
        self.assertEqual(driver_store.signing("Realtek Semiconductor Corp."), "maker")
        self.assertIsNone(driver_store.signing(""))
        self.assertEqual(driver_store.parse_packages(_XML)[0]["signer"], "Microsoft Windows Hardware Compatibility Publisher")

    def test_windows_own_check_of_a_package(self):
        # SetupVerifyInfFile, faked: trusted on its own; signed only by its maker (the check fails but names
        # the maker); not signed at all; not readable (no answer)
        answers = {"oem1.inf": (True, "Microsoft Windows Hardware Compatibility Publisher", 0),
                   "oem2.inf": (False, "Acme Corp", 0xE0000242), "oem3.inf": (False, "", 0xE000022F),
                   "oem4.inf": (False, "", 5)}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(driver_store, "INF_DIR", Path(folder)), \
                mock.patch.object(driver_store, "_signers", {}), \
                mock.patch.object(driver_store, "_verify_inf", side_effect=lambda p: answers[p.name]) as verify:
            for name in answers:
                Path(folder, name).write_text("[Version]\n", encoding="utf-8")
            got = {name: driver_store.inf_signer(name) for name in answers}
            driver_store.inf_signer("oem1.inf")
            self.assertEqual(verify.call_count, 4)  # once per package
            self.assertEqual(driver_store.inf_signer("oem9.inf"), (None, None))  # no such package
        self.assertEqual(got, {"oem1.inf": ("Microsoft Windows Hardware Compatibility Publisher", "microsoft"),
                               "oem2.inf": ("Acme Corp", "maker"), "oem3.inf": (None, "unsigned"),
                               "oem4.inf": (None, None)})

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_windows_own_check_on_this_pc(self):
        # Read-only: an INF that came with Windows is signed by Windows itself
        with mock.patch.object(driver_store, "_signers", {}):
            self.assertEqual(driver_store.inf_signer("machine.inf"), ("Microsoft Windows", "windows"))

    def test_every_package_gets_its_signer(self):
        def scan(pnputil, inf, signer):
            with mock.patch.object(driver_store, "IS_WINDOWS", True), \
                    mock.patch.object(driver_store, "store_folder", return_value=None), \
                    mock.patch.object(driver_store, "_added", return_value=None), \
                    mock.patch.object(driver_store, "_pnputil_packages", return_value=pnputil), \
                    mock.patch.object(driver_store, "_inf_packages", return_value=inf), \
                    mock.patch.object(driver_store, "inf_signer", side_effect=signer):
                return driver_store.packages([])["packages"][0]

        named = scan([{**package("oem10.inf", "a.inf", "1", "2026-01-01"), "signer": "Microsoft Windows"}], None,
                     AssertionError("pnputil named it"))
        self.assertEqual(named["signing"], "windows")
        checked = scan(None, [{**package("oem11.inf", "b.inf", "1", "2026-01-01"), "signer": None}],
                       lambda name: ("Acme Corp", "maker"))  # older Windows: Windows' own check
        self.assertEqual((checked["signer"], checked["signing"]), ("Acme Corp", "maker"))

    def test_inf_version_section(self):
        text = ("[Version]\nSignature=\"$WINDOWS NT$\"\nClass=Net ; network\nClassGuid={4d36e972-e325-11ce-bfc1-08002be10318}\n"
                "Provider=%Realtek%\nDriverVer=07/14/2016,10.10.714.2016\n\n[Strings]\nRealtek=\"Realtek; Semiconductor\"\n")
        v = driver_store.inf_version_section(text)
        self.assertEqual(v, {"provider": "Realtek; Semiconductor", "class": "Net",
                             "classguid": "{4d36e972-e325-11ce-bfc1-08002be10318}", "driverver": "07/14/2016,10.10.714.2016"})


# ------------------------------------------------------------ drivers apps added

def service(name, file, running=True, start="manual"):
    return {"name": name, "display_name": name, "running": running, "start": start, "file_system": False, "file": file}


class SoftwareDriverTests(unittest.TestCase):
    def test_image_paths(self):
        root = software_drivers._SYSTEM_ROOT
        self.assertEqual(software_drivers.image_path(r"\SystemRoot\System32\drivers\x.sys", "x"),
                         root + r"\System32\drivers\x.sys")
        self.assertEqual(software_drivers.image_path(r"System32\drivers\y.sys", "y"), root + r"\System32\drivers\y.sys")
        self.assertEqual(software_drivers.image_path(r"\??\C:\Program Files\App\z.sys", "z"), r"C:\Program Files\App\z.sys")
        self.assertEqual(software_drivers.image_path(None, "w"), root + r"\System32\drivers\w.sys")

    def test_categories(self):
        self.assertEqual(software_drivers.category("vgk", r"C:\Program Files\Riot Vanguard\vgk.sys", "Riot Games"), "Anti-cheat")
        self.assertEqual(software_drivers.category("WinRing0_1_2_0", r"C:\FanControl\WinRing0x64.sys", ""), "Hardware control")
        self.assertEqual(software_drivers.category("wintun", r"C:\x\wintun.sys", "WireGuard LLC"), "VPN and network")
        self.assertEqual(software_drivers.category("foo", r"C:\x\foo.sys", "Acme"), "Other")

    def test_what_is_listed(self):
        drivers_dir = software_drivers._SYSTEM_ROOT + r"\System32\drivers"
        services = [
            service("ms", drivers_dir + r"\ms.sys"),  # Windows' own
            service("nvlddmkm", r"C:\Windows\System32\DriverStore\FileRepository\nv_dispi.inf_amd64_1\nvlddmkm.sys"),
            service("inbox", r"C:\Windows\System32\DriverStore\FileRepository\inbox.inf_amd64_2\inbox.sys"),
            service("lsi_sas", drivers_dir + r"\lsi_sas.sys", running=False, start="boot"),  # no such hardware here
            service("anticheat", r"C:\Program Files\Game\ac.sys"),
            service("gone", r"C:\Program Files\Old\gone.sys", running=False, start="system"),
            service("gone_manual", r"C:\Program Files\Old\g2.sys", running=False, start="manual"),
            service("WinRing0_1_2_0", r"C:\Tools\WinRing0x64.sys", running=False),
        ]
        details = {
            "ms.sys": {"company": "Microsoft Corporation"}, "nvlddmkm.sys": {"company": "NVIDIA Corporation"},
            "ac.sys": {"company": "Game Security Ltd", "description": "Anti-cheat"},
            "winring0x64.sys": {"company": "OpenLibSys.org"}, "inbox.sys": {"company": ""}, "lsi_sas.sys": {"company": "LSI"},
        }
        read = []

        def details_of(path):
            read.append(path)
            return details.get(path.rsplit("\\", 1)[-1].lower())

        with mock.patch("os.path.exists", return_value=True):
            out = software_drivers.pick(services, {"nvlddmkm"}, {"nv_dispi.inf_amd64_1"}, details_of)
        names = [d["name"] for d in out]
        self.assertEqual(names, ["WinRing0_1_2_0", "gone", "anticheat"])  # the risk first, then left behind
        self.assertTrue(out[0]["risk"])
        self.assertTrue(out[1]["leftover"])
        self.assertNotIn(drivers_dir + r"\lsi_sas.sys", read)  # skipped without reading the file
        self.assertFalse(any("inbox.sys" in p for p in read))  # a package that came with Windows


# ------------------------------------------------------------ Windows' logs

def event(provider, event_id, data, unnamed=()):
    named = "".join(f'<Data Name="{k}">{v}</Data>' for k, v in data.items())
    plain = "".join(f"<Data>{v}</Data>" for v in unnamed)
    return (f'<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><Provider Name="{provider}"/>'
            f'<EventID>{event_id}</EventID><TimeCreated SystemTime="2026-10-18T10:11:47.1234567Z"/></System>'
            f"<EventData>{named}{plain}</EventData></Event>")


class EventTests(unittest.TestCase):
    def classify(self, xml, names=None):
        fields = driver_events.event_fields(xml)
        self.assertEqual(fields["time"].isoformat(), "2026-10-18T10:11:47.123456+00:00")
        return driver_events.classify(fields, names or {})

    def test_driver_events(self):
        e = self.classify(event("Microsoft-Windows-Kernel-PnP", 219, {"DeviceInstance": "SWD\\Sensors\\1",
                                                                          "DriverName": "\\Driver\\WudfRd"}))
        self.assertEqual((e["kind"], e["subject"], e["device"]), ("failed_to_load", "WudfRd", "SWD\\Sensors\\1"))
        e = self.classify(event("Service Control Manager", 7026, {"param1": "\n dam\n acme"}))
        self.assertEqual((e["kind"], e["names"]), ("boot_driver_not_loaded", ["dam", "acme"]))
        self.assertIsNone(self.classify(event("Service Control Manager", 7000, {"param1": "Print Spooler", "param2": "x"})))
        e = self.classify(event("Service Control Manager", 7000, {"param1": "Acme Filter", "param2": "The system cannot "
                                                                  "find the file specified."}), {"acme filter": "acmeflt"})
        self.assertEqual((e["kind"], e["subject"]), ("service_not_started", "acmeflt"))
        e = self.classify(event("Microsoft-Windows-Kernel-PnP", 411, {"DeviceInstanceId": "USB\\VID_1\\2", "Problem": "0x2b",
                                                                      "Status": "0xC00000E5"}))
        self.assertEqual((e["kind"], e["problem"]), ("device_problem", 43))

    def test_code_integrity(self):
        audit = self.classify(event("Microsoft-Windows-CodeIntegrity", 3076, {
            "FileNameBuffer": "\\Device\\HarddiskVolume4\\Windows\\System32\\drivers\\oldtool.sys",
            "PolicyIdBuffer": "{784C4414-79F4-4C32-A6A5-F0FB42A51D0D}"}))
        self.assertEqual((audit["kind"], audit["subject"], audit["policy"]),
                         ("would_be_blocked", "oldtool.sys", driver_events.DRIVER_POLICY_EVALUATION))
        blocked = self.classify(event("Microsoft-Windows-CodeIntegrity", 3077, {
            "FileNameBuffer": "\\Device\\HarddiskVolume4\\x\\psmounterex.sys",
            "PolicyGUID": "{D2BDA982-CCF6-4344-AC5B-0B44427B6816}"}))
        self.assertEqual((blocked["kind"], blocked["policy_name"]), ("blocked", "the Microsoft Vulnerable Driver Blocklist"))
        # The same events record DLLs a protected process refused: not about drivers
        self.assertIsNone(self.classify(event("Microsoft-Windows-CodeIntegrity", 3033, {
            "FileNameBuffer": "\\Device\\HarddiskVolume4\\Windows\\System32\\nvspcap64.dll"})))
        self.assertEqual(self.classify(event("Microsoft-Windows-CodeIntegrity", 3033, {
            "FileNameBuffer": "\\Device\\HarddiskVolume4\\x\\bad.sys"}))["kind"], "signature_refused")

    def test_devices_that_stopped_responding(self):
        e = self.classify(event("Microsoft-Windows-NDIS", 10400, {"AdapterName": "Realtek PCIe GBE Family Controller",
                                                                   "ResetReason": "2", "ResetCount": "2429"}))
        self.assertEqual((e["kind"], e["subject"]), ("network_reset", "Realtek PCIe GBE Family Controller"))
        e = self.classify(event("storahci", 129, {}, ["\\Device\\RaidPort0"]))
        self.assertEqual((e["kind"], e["subject"], e["port"]), ("storage_reset", "storahci", "\\Device\\RaidPort0"))
        e = self.classify(event("disk", 153, {}, ["\\Device\\Harddisk0\\DR0", "0x5d6790", "0", "\\Device\\00000036"]))
        self.assertEqual((e["kind"], e["subject"], e["disk"]), ("disk_retry", "Disk 0", 0))
        e = self.classify(event("nvlddmkm", 153, {}, ["\\Device\\000000bb", "Error occurred on GPUID: 100"]))
        self.assertEqual((e["kind"], e["subject"]), ("gpu_error", "nvlddmkm"))
        e = self.classify(event("Display", 4101, {}, ["amdkmdag", ""]))
        self.assertEqual((e["kind"], e["subject"]), ("gpu_error", "amdkmdag"))

    def test_grouping(self):
        t1, t2 = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 18, tzinfo=timezone.utc)
        item = {"kind": "boot_driver_not_loaded", "subject": "dam", "names": ["dam"]}
        out = driver_events.summarize([(t2, dict(item)), (t1, dict(item))])
        self.assertEqual((len(out), out[0]["count"], out[0]["first"][:10], out[0]["last"][:10]), (1, 2, "2026-10-01", "2026-10-18"))

    def test_policy_mode(self):
        self.assertEqual(driver_events.driver_policy_mode({driver_events.DRIVER_POLICY_EVALUATION}), "evaluation")
        self.assertEqual(driver_events.driver_policy_mode({driver_events.DRIVER_POLICY_EVALUATION,
                                                           driver_events.DRIVER_POLICY_ENFORCED}), "enforced")
        self.assertEqual(driver_events.driver_policy_mode(set()), "off")

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_protection_state_needs_no_rights(self):
        p = driver_events.protection()
        self.assertEqual(set(p) - {"checked_at"}, {"driver_policy", "blocklist", "memory_integrity", "test_signing",
                                                   "integrity_checks_off", "windows_update_drivers"})


# ------------------------------------------------------------ update checks

_PRODUCTS = """<?xml version="1.0" encoding="UTF-8"?><LookupValueSearch><LookupValues>
<LookupValue ParentID="107"><Name>GeForce RTX 2060 SUPER</Name><Value>902</Value></LookupValue>
<LookupValue ParentID="131"><Name>NVIDIA GeForce RTX 5070</Name><Value>1070</Value></LookupValue>
<LookupValue ParentID="133"><Name>NVIDIA GeForce RTX 5070 Laptop GPU</Name><Value>1077</Value></LookupValue>
</LookupValues></LookupValueSearch>"""
_LOOKUP = {"Success": "1", "IDS": [{"downloadInfo": {
    "Version": "617.42", "ReleaseDateTime": "Tue Oct 06, 2026", "Name": "GeForce%20Game%20Ready%20Driver",
    "DetailsURL": "https://www.nvidia.com/en-us/drivers/details/280103/",
    "DownloadURL": "https://us.download.nvidia.com/Windows/617.42/617.42-desktop-win10-win11-64bit-international-dch-whql.exe",
    "DownloadURLFileSize": "990.85 MB"}}]}


class UpdateTests(unittest.TestCase):
    def test_nvidia_versions(self):
        self.assertEqual(driver_updates.nvidia_version("32.0.16.1742"), "617.42")
        self.assertEqual(driver_updates.nvidia_version("32.0.15.6094"), "560.94")
        self.assertEqual(driver_updates.nvidia_version("27.21.14.5671"), "456.71")
        self.assertEqual(driver_updates.nvidia_version("32.0.16.742"), "607.42")
        self.assertIsNone(driver_updates.nvidia_version("1.4.6.3x"))
        self.assertGreater(driver_updates.version_tuple("617.42"), driver_updates.version_tuple("617.14"))

    def test_nvidia_product_list(self):
        products = driver_updates.parse_products(_PRODUCTS)
        self.assertEqual(driver_updates.find_product(products, "NVIDIA GeForce RTX 2060 SUPER"), (902, 107))
        self.assertEqual(driver_updates.find_product(products, "NVIDIA GeForce RTX 5070"), (1070, 131))
        self.assertEqual(driver_updates.find_product(products, "NVIDIA GeForce RTX 5070 Laptop GPU"), (1077, 133))
        self.assertIsNone(driver_updates.find_product(products, "NVIDIA GeForce GTX 1060"))

    def test_nvidia_answer(self):
        found = driver_updates.parse_lookup(_LOOKUP)
        self.assertEqual((found["latest"], found["date"], found["name"]), ("617.42", "2026-10-06", "GeForce Game Ready Driver"))
        self.assertTrue(found["download_url"].startswith("https://us.download.nvidia.com/"))
        forged = {"IDS": [{"downloadInfo": {**_LOOKUP["IDS"][0]["downloadInfo"],
                                            "DownloadURL": "https://example.com/617.42.exe",
                                            "DetailsURL": "https://example.com/notes"}}]}
        found = driver_updates.parse_lookup(forged)
        self.assertIsNone(found["download_url"])  # only NVIDIA's own download server
        self.assertIsNone(found["details_url"])
        self.assertIsNone(driver_updates.parse_lookup({"IDS": [{"downloadInfo": {"Version": "latest"}}]}))
        self.assertFalse(driver_updates.is_nvidia_download("http://us.download.nvidia.com/x.exe"))  # not https

    def test_nvidia_check_is_kept_and_errors_retried_later(self):
        with mock.patch.object(driver_updates, "_products", return_value=driver_updates.parse_products(_PRODUCTS)), \
                mock.patch.object(driver_updates, "_http_get", return_value=__import__("json").dumps(_LOOKUP).encode()) as get:
            first = driver_updates.check_nvidia("NVIDIA GeForce RTX 2060 SUPER", "32.0.16.1714", force=True)
            again = driver_updates.check_nvidia("NVIDIA GeForce RTX 2060 SUPER", "32.0.16.1742")
        self.assertEqual((first["installed"], first["latest"], first["newer"]), ("617.14", "617.42", True))
        self.assertEqual(get.call_count, 1)  # the second look used the kept answer...
        self.assertFalse(again["newer"])  # ...with the driver installed since
        with mock.patch.object(driver_updates, "_products", side_effect=OSError("offline")):
            failed = driver_updates.check_nvidia("NVIDIA GeForce RTX 2060 SUPER", "32.0.16.1742", force=True)
        self.assertIn("couldn't reach NVIDIA", failed["error"])

    def test_download_folder_is_locked_to_administrators(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "OptiMind", "downloads")
            calls = []
            with mock.patch.object(driver_updates, "DOWNLOADS", folder), \
                    mock.patch.object(driver_updates.subprocess, "run",
                                      side_effect=lambda args, **kw: calls.append(args) or mock.Mock(returncode=0)):
                self.assertEqual(driver_updates.secure_downloads(), folder)
            self.assertTrue(folder.is_dir())
            self.assertEqual([c[2:4] for c in calls], [["/setowner", "*S-1-5-32-544"], ["/inheritance:r", "/grant:r"]] * 2)
            self.assertIn("*S-1-5-32-545:(OI)(CI)RX", calls[1])  # standard users may only read
            with mock.patch.object(driver_updates, "DOWNLOADS", folder), \
                    mock.patch.object(driver_updates, "_is_link", return_value=True):
                with self.assertRaises(driver_updates.UpdateError):
                    driver_updates.secure_downloads()

    def test_only_an_installer_signed_by_nvidia_starts(self):
        info = {**driver_updates.parse_lookup(_LOOKUP)}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            exe = folder / "617.42-desktop-win10-win11-64bit-international-dch-whql.exe"
            planted = folder / "version.dll"

            def run(signer):
                exe.write_bytes(b"MZ")  # already downloaded: nothing is fetched
                planted.write_bytes(b"x")
                with mock.patch.object(driver_updates, "secure_downloads", return_value=folder), \
                        mock.patch.object(driver_updates, "signer_of", return_value=signer), \
                        mock.patch.object(driver_updates.subprocess, "Popen") as popen:
                    try:
                        return driver_updates.install_nvidia(info, driver_jobs.Job("t", "t")), popen
                    except driver_updates.UpdateError as e:
                        return e, popen

            failed, popen = run(("HashMismatch", "CN=NVIDIA Corporation, O=NVIDIA Corporation"))
            self.assertIsInstance(failed, driver_updates.UpdateError)
            self.assertFalse(exe.exists())  # deleted
            popen.assert_not_called()
            done, popen = run(("Valid", "CN=NVIDIA Corporation, O=NVIDIA Corporation, L=Santa Clara, C=US"))
            self.assertEqual(done["version"], "617.42")
            popen.assert_called_once_with([str(exe)], cwd=str(folder))
            self.assertFalse(planted.exists())  # nothing else stays next to it

    def test_windows_update_offer(self):
        class UpdateIdentity:
            UpdateID = "B2B1A9C1-0D5C-4C83-9E6A-0123456789AB"

        class Update:
            Identity = UpdateIdentity()
            Title = "Realtek - Net - 10.77.825.2026"
            DriverVerDate = datetime(2026, 8, 25)
            DriverClass, DriverProvider, DriverManufacturer = "Net", "Realtek", "Realtek"
            DriverModel = "Realtek PCIe GbE Family Controller"
            DriverHardwareID = "PCI\\VEN_10EC&DEV_8168"
            MaxDownloadSize, BrowseOnly = 2_500_000, True

        u = driver_updates.update_info(Update())
        self.assertEqual((u["id"], u["version"], u["date"], u["hardware_id"], u["optional"]),
                         ("b2b1a9c1-0d5c-4c83-9e6a-0123456789ab", "10.77.825.2026", "2026-08-25", "pci\\ven_10ec&dev_8168", True))

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_windows_update_never_installs_firmware(self):
        # The installer checks what Windows Update itself says about each update (faked here): firmware, or
        # anything that isn't a driver, stops it before anything is downloaded or installed
        class Updates:
            def __init__(self, items):
                self.items, self.Count = items, len(items)

            def Item(self, i):
                return self.items[i]

        for cls, kind in (("Firmware", 2), ("Net", 1)):
            session = mock.Mock()
            update = mock.Mock(Title=f"Maker - {cls} - 1.0", Type=kind, DriverClass=cls, EulaAccepted=True)
            session.CreateUpdateSearcher.return_value.Search.return_value = mock.Mock(Updates=Updates([update]))
            with self.subTest(cls), mock.patch("win32com.client.Dispatch",
                                               side_effect=lambda name, s=session: s if name.endswith("Session") else mock.Mock()):
                with self.assertRaises(driver_updates.UpdateError) as e:
                    driver_updates._install(["b2b1a9c1-0d5c-4c83-9e6a-0123456789ab"], mock.Mock())
                self.assertIn("never installs firmware", str(e.exception))
                session.CreateUpdateDownloader.assert_not_called()
                session.CreateUpdateInstaller.assert_not_called()


# ------------------------------------------------------------ findings

FW = {"board": {"maker": "ASUSTeK COMPUTER INC.", "model": "PRIME B450M-GAMING/BR"}, "system": {"maker": "", "model": ""},
      "bios": {"vendor": "AMI", "version": "4631", "date": "2025-01-14"},
      "support": {"label": "ASUS Download Center", "url": "https://www.asus.com/support/download-center/"}}


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.gpu = device("NVIDIA GeForce RTX 2060 SUPER", "Display", "NVIDIA", "32.0.16.1742", "2026-09-30", "oem37.inf",
                          service="nvlddmkm", instance="PCI\\VEN_10DE&DEV_1F06\\4&1")
        self.sound = device("Realtek High Definition Audio", "MEDIA", "Realtek Semiconductor Corp.", "6.0.1.7841",
                            "2016-06-03", "oem22.inf", bus="HDAUDIO")
        self.lan = device("Realtek PCIe GBE Family Controller", "Net", "Realtek", "10.10.714.2016", "2016-07-14", "oem23.inf",
                          instance="PCI\\VEN_10EC&DEV_8168\\4&2")
        self.gpio = device("AMD GPIO Controller", "System", bus="ACPI")
        self.ahci = device("Standard SATA AHCI Controller", "HDC", "Microsoft", "10.0.26100.1", "2006-06-21", "mshdc.inf")

    def test_key_drivers_and_where_they_come_from(self):
        keys = drivers.key_drivers([self.gpu, self.sound, self.lan, self.gpio, self.ahci], FW, TODAY)
        by = {k["device"]: k for k in keys}
        self.assertEqual([k["part"] for k in keys], ["Graphics", "Chipset", "Sound", "Network", "Storage"])
        self.assertEqual(by["Realtek High Definition Audio"]["status"], "old")
        self.assertEqual(by["Standard SATA AHCI Controller"]["status"], "windows")
        self.assertEqual(by["NVIDIA GeForce RTX 2060 SUPER"]["source"]["url"], "https://www.nvidia.com/en-us/drivers/")
        self.assertTrue(by["AMD GPIO Controller"]["source"]["url"].endswith("/chipsets/am4/b450.html"))
        self.assertEqual(by["Realtek PCIe GBE Family Controller"]["source"]["url"], drivers.LINKS["realtek"][1])
        self.assertEqual(by["Realtek High Definition Audio"]["source"]["url"], FW["support"]["url"])
        old = drivers._old_driver_findings(keys, FW)
        self.assertEqual(len(old[0]["items"]), 2)
        self.assertIn("PRIME B450M-GAMING/BR", old[0]["action"])

    def test_basic_display_driver(self):
        basic = device("Microsoft Basic Display Adapter", "Display", "Microsoft", "10.0.26100.1", "2006-06-21",
                       "display.inf", service="BasicDisplay", instance="PCI\\VEN_10DE&DEV_2484\\4&9")
        keys = drivers.key_drivers([basic], FW, TODAY)
        self.assertEqual(keys[0]["status"], "warn")
        found = drivers._graphics_findings(keys)
        self.assertEqual((found[0]["status"], found[0]["link"]), ("warn", "https://www.nvidia.com/en-us/drivers/"))

    def test_device_problems(self):
        broken = device("USB Wi-Fi", "Net", problem=43, bus="USB")
        off = device("Webcam", "Camera", problem=22, bus="USB")
        missing = device("Unknown device", "", provider="", version="", when=None, inf="", problem=28)
        unplugged = device("Old stick", "DiskDrive", present=False)
        found = {f["title"]: f for f in drivers._device_findings([broken, off, missing, unplugged])}
        self.assertEqual(found["USB Wi-Fi"]["status"], "warn")
        self.assertIn("restart_device", [a["do"] for a in found["USB Wi-Fi"]["actions"]])
        self.assertEqual(found["Webcam"]["status"], "info")
        self.assertEqual(found["Webcam"]["actions"][0]["do"], "enable_device")
        self.assertEqual({a["do"] for a in found["Unknown device"]["actions"]}, {"scan_hardware", "check_windows_update"})
        self.assertNotIn("Old stick", found)

    def test_protection(self):
        base = {"driver_policy": "evaluation", "blocklist": True, "memory_integrity": False, "test_signing": False,
                "integrity_checks_off": False, "windows_update_drivers": True}
        self.assertEqual([f["status"] for f in drivers._protection_findings(base, [])], ["good"])
        flagged = [{"kind": "would_be_blocked", "subject": "oldtool.sys", "policy": driver_events.DRIVER_POLICY_EVALUATION,
                    "count": 3, "last": "2026-10-18T10:00:00+00:00"}]
        f = drivers._protection_findings(base, flagged)[0]
        self.assertEqual((f["status"], f["items"][0][:11]), ("warn", "oldtool.sys"))
        risky = {**base, "blocklist": False, "test_signing": True, "windows_update_drivers": False}
        ids = {f["id"]: f["status"] for f in drivers._protection_findings(risky, [])}
        self.assertEqual((ids["blocklist"], ids["test_signing"], ids["wu_drivers_excluded"]), ("warn", "warn", "info"))
        self.assertNotIn("blocklist", {f["id"] for f in drivers._protection_findings(
            {**base, "blocklist": False, "memory_integrity": True}, [])})  # Memory integrity enforces it anyway

    def test_several_devices_resetting_at_once(self):
        def group(kind, subject, count, first, last, **extra):
            return {"kind": kind, "subject": subject, "count": count, "first": first, "last": last, **extra}

        night = [group("network_reset", "Realtek PCIe GBE Family Controller", 2429, "2026-10-19T04:30:37+00:00",
                       "2026-10-19T13:10:10+00:00"),
                 group("gpu_error", "nvlddmkm", 140, "2026-10-19T04:38:00+00:00", "2026-10-19T13:10:00+00:00"),
                 group("disk_retry", "Disk 0", 44, "2026-10-19T04:31:22+00:00", "2026-10-19T13:03:00+00:00", disk=0),
                 group("storage_reset", "storahci", 18, "2026-10-18T20:29:00+00:00", "2026-10-19T13:10:00+00:00",
                       port="\\Device\\RaidPort0")]
        keys = drivers.key_drivers([self.lan], FW, TODAY)
        boot = datetime(2026, 10, 19, 13, 13, 56, tzinfo=timezone.utc)
        now = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
        f = drivers._reset_findings(night, keys, FW, boot, {0: "WDC WD10SPZX-21Z10T0"}, now)[0]
        self.assertEqual(f["status"], "warn")
        self.assertIn("4 devices stopped responding and had to be reset during the same hours", f["summary"])
        self.assertIn("stopped after the last restart", f["summary"])
        self.assertTrue(f["items"][0].startswith("Realtek PCIe GBE Family Controller: reset 2,429 times"))
        self.assertTrue(any("Disk 0 (WDC WD10SPZX-21Z10T0)" in i for i in f["items"]))
        self.assertTrue(any("Storage controller (storahci, RaidPort0): reset 18 times" in i for i in f["items"]))
        self.assertIn("something they share", f["detail"])
        self.assertIn("Update the Realtek PCIe GBE Family Controller driver: yours is from July 2016", f["action"])
        self.assertIn("SATA data and power cables", f["action"])
        self.assertIn("AMD B450 chipset drivers", f["action"])
        one = drivers._reset_findings(night[1:2], keys, FW, boot, {}, now + timedelta(days=20))[0]
        self.assertEqual(one["status"], "info")  # long past
        self.assertIn("A device that keeps resetting", one["detail"])
        self.assertEqual(drivers._reset_findings([], keys, FW, boot, {}, now), [])

    def test_events_from_other_makers_only(self):
        events = [{"kind": "boot_driver_not_loaded", "subject": "dam", "names": ["dam"], "count": 27, "last": "2026-10-19T00:00:00"},
                  {"kind": "failed_to_load", "subject": "acmeflt", "count": 2, "last": "2026-10-18T00:00:00"},
                  {"kind": "device_problem", "subject": self.lan["id"], "problem": 10, "count": 1, "last": "2026-10-10T00:00:00"}]
        found = {f["id"]: f for f in drivers._event_findings(events, {"dam": True, "acmeflt": False}, [self.lan])}
        self.assertEqual(len(found["failed_drivers"]["items"]), 1)  # Windows' own "dam" isn't a finding
        self.assertIn("Realtek PCIe GBE Family Controller", found["device_hiccups"]["items"][0])

    def test_home_never_looks_for_backups(self):
        report = {"findings": [], "devices": [], "key_drivers": [], "summary": {}, "checked_at": "x"}
        with mock.patch.object(driver_updates, "last_results", return_value={"windows_update": None, "nvidia": None}):
            self.assertEqual(drivers._with_updates(report, {}, None)["findings"], [])  # not looked for: no finding
            self.assertEqual(drivers._with_updates(report, {}, [])["findings"][0]["id"], "backup")
        with mock.patch.object(driver_actions, "_known_backups", [{"path": "D:\\x"}]), \
                mock.patch.object(driver_actions.psutil, "disk_partitions", side_effect=AssertionError("no disk access")):
            self.assertEqual(driver_actions.backups(refresh=False), [{"path": "D:\\x"}])

    def test_store_firmware_and_backup(self):
        pk = [{**package("oem1.inf", "nv_dispi.inf", "1", "2024-01-01", size=2 ** 30), "removable": True},
              {**package("oem2.inf", "nv_dispi.inf", "2", "2025-01-01"), "removable": False}]
        self.assertEqual(drivers._store_findings(pk)[0]["status"], "tip")
        bios = drivers._firmware_findings(FW, TODAY)[0]
        self.assertIn("BIOS 4631 from January 2025 (21 months ago)", bios["summary"])
        self.assertIn("never updates a BIOS", bios["action"])
        self.assertEqual(drivers._backup_findings([])[0]["status"], "tip")
        self.assertEqual(drivers._backup_findings([{"this_pc": True, "created": "2026-10-19T10:00:00+00:00", "packages": 37,
                                                    "path": "D:\\OptiMind driver backup\\2026-10-19 10.00"}])[0]["status"], "good")

    def test_windows_update_offers_next_to_the_installed_driver(self):
        wu = {"updates": [
            {"id": "a", "title": "Realtek - Net - 10.77", "hardware_id": "pci\\ven_10ec&dev_8168", "date": "2026-08-25", "version": "10.77"},
            {"id": "b", "title": "Realtek - Net - old", "hardware_id": "pci\\ven_10ec&dev_8168", "date": "2015-01-01", "version": "9.1"},
            {"id": "c", "title": "Something", "hardware_id": "usb\\vid_9999", "date": "2026-01-01", "version": "1"}]}
        items = {i["id"]: i for i in drivers.update_items(wu, [self.lan], {self.lan["id"]: ["pci\\ven_10ec&dev_8168"]})}
        self.assertEqual((items["a"]["verdict"], items["b"]["verdict"], items["c"]["verdict"]), ("newer", "older", "unknown"))
        self.assertEqual(items["a"]["installed_version"], "10.10.714.2016")
        found = drivers._update_findings(list(items.values()), wu, None, [])
        self.assertEqual(found[0]["items"], ["Realtek - Net - 10.77", "Something"])  # never the older one

    def test_firmware_is_never_offered_for_install(self):
        wu = {"updates": [
            {"id": "f", "title": "ASUSTeK COMPUTER INC. - Firmware - 4.6.31", "class": "Firmware",
             "hardware_id": "uefi\\res_{0}", "date": "2026-05-01", "version": "4.6.31"},
            {"id": "g", "title": "Lenovo Ltd. - Firmware - 1.2", "class": "", "hardware_id": "", "date": "2026-05-01",
             "version": "1.2"},  # the class only in the title
            {"id": "a", "title": "Realtek - Net - 10.77", "class": "Net", "hardware_id": "pci\\ven_10ec&dev_8168",
             "date": "2026-08-25", "version": "10.77"}]}
        items = {i["id"]: i for i in drivers.update_items(wu, [self.lan], {self.lan["id"]: ["pci\\ven_10ec&dev_8168"]})}
        self.assertEqual((items["f"]["verdict"], items["g"]["verdict"], items["a"]["verdict"]),
                         ("firmware", "firmware", "newer"))
        found = {f["id"]: f for f in drivers._update_findings(list(items.values()), wu, None, [])}
        self.assertEqual(found["windows_update"]["items"], ["Realtek - Net - 10.77"])  # never the firmware
        self.assertEqual((found["firmware_offer"]["status"], len(found["firmware_offer"]["items"])), ("info", 2))
        self.assertIn("doesn't install firmware", found["firmware_offer"]["summary"])
        self.assertFalse(driver_updates.is_firmware({"class": "System", "title": "Intel - System - 2406.5.5.0"}))

    def test_who_signed_each_driver(self):
        packages = [{"name": "oem37.inf", "signer": "Microsoft Windows Hardware Compatibility Publisher",
                     "signing": "microsoft"},
                    {"name": "oem22.inf", "signer": "Realtek Semiconductor Corp.", "signing": "maker"},
                    {"name": "oem23.inf", "signer": None, "signing": "unsigned"}]
        drivers.add_signers([self.gpu, self.sound, self.lan, self.ahci], packages)
        self.assertEqual((self.ahci["driver"]["signer"], self.ahci["driver"]["signing"]),
                         ("Microsoft Windows", "windows"))  # mshdc.inf came with Windows
        keys = {k["device"]: k for k in drivers.key_drivers([self.gpu, self.sound], FW, TODAY)}
        self.assertEqual(keys["NVIDIA GeForce RTX 2060 SUPER"]["signing"], "microsoft")
        self.assertEqual(keys["Realtek High Definition Audio"]["signer"], "Realtek Semiconductor Corp.")
        f = drivers._signature_findings([self.gpu, self.sound, self.lan, self.ahci])[0]
        self.assertEqual(f["status"], "tip")  # one isn't signed at all
        self.assertIn("2 drivers your devices use aren't signed", f["summary"])
        self.assertEqual(f["items"], [
            "Realtek PCIe GBE Family Controller: Realtek 10.10.714.2016, not signed",
            "Realtek High Definition Audio: Realtek Semiconductor Corp. 6.0.1.7841, signed only by Realtek Semiconductor "
            "Corp."])
        self.assertEqual(drivers._signature_findings([self.gpu, self.sound])[0]["status"], "info")  # only the maker's
        good = drivers._signature_findings([self.gpu, self.ahci])[0]
        self.assertEqual(good["status"], "good")
        self.assertTrue(good["summary"].startswith("The one driver from other makers"))
        self.assertEqual(drivers._signature_findings([self.ahci]), [])  # only Windows' own: nothing to say

    def test_nvidia_findings(self):
        keys = drivers.key_drivers([self.gpu], FW, TODAY)
        newer = {"gpu": self.gpu["name"], "installed": "617.14", "latest": "617.42", "date": "2026-10-06", "newer": True,
                 "details_url": "https://www.nvidia.com/en-us/drivers/details/280103/"}
        f = drivers._update_findings([], None, newer, keys)[0]
        self.assertEqual((f["status"], f["actions"][0]["do"]), ("tip", "install_nvidia"))
        current = {**newer, "installed": "617.42", "newer": False}
        self.assertEqual(drivers._update_findings([], None, current, keys)[0]["status"], "good")
        offline = {"gpu": self.gpu["name"], "installed": "617.42", "error": "couldn't reach NVIDIA (ConnectError)"}
        self.assertEqual(drivers._update_findings([], None, offline, keys)[0]["status"], "info")

    def test_makers(self):
        self.assertEqual(drivers.maker_support("Micro-Star International Co., Ltd.")["label"], "MSI Download Center")
        self.assertIsNone(drivers.maker_support("System manufacturer"))
        self.assertEqual(drivers.amd_chipset_link("ROG STRIX X570-E GAMING")[1],
                         "https://www.amd.com/en/support/downloads/drivers.html/chipsets/am4/x570.html")
        self.assertEqual(drivers.amd_chipset_link("PRO B650-P")[1], drivers.LINKS["amd"][1])  # AM5: AMD's own finder


# ------------------------------------------------------------ the tools (never run for real here)

class ActionTests(unittest.TestCase):
    def setUp(self):
        self.lan = device("Realtek PCIe GBE Family Controller", "Net", problem=43, instance="PCI\\VEN_10EC&DEV_8168\\4&2")
        self.cam = device("Webcam", "Camera", problem=22, instance="USB\\VID_1&PID_2\\1")
        patches = [mock.patch.object(driver_actions.devices_mod, "read_devices", return_value=[self.lan, self.cam]),
                   mock.patch.object(driver_actions, "pnputil", side_effect=AssertionError("pnputil must not run")),
                   mock.patch.object(driver_actions.subprocess, "Popen", side_effect=AssertionError("nothing may start"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_needs_administrator_rights(self):
        with mock.patch.object(driver_actions, "is_elevated", return_value=False):
            with self.assertRaises(driver_actions.DriverActionError) as e:
                driver_actions.scan_hardware()
        self.assertEqual(e.exception.status, 403)

    def test_test_mode_only_says_what_it_would_do(self):
        with mock.patch.object(driver_actions, "is_elevated", return_value=True):
            self.assertTrue(driver_actions.scan_hardware().startswith("simulated"))
            self.assertIn("restart Realtek", driver_actions.restart_device(self.lan["id"].lower()))
            self.assertIn("turn on Webcam", driver_actions.enable_device(self.cam["id"]))
            with self.assertRaises(driver_actions.DriverActionError):
                driver_actions.enable_device(self.lan["id"])  # it isn't turned off
            with self.assertRaises(driver_actions.DriverActionError) as e:
                driver_actions.restart_device("PCI\\NOPE\\1")
            self.assertEqual(e.exception.status, 404)

    def test_only_old_versions_can_be_removed(self):
        pk = [{**package("oem1.inf", "x.inf", "1", "2020-01-01", size=5 * 2 ** 20), "removable": True},
              {**package("oem2.inf", "x.inf", "2", "2026-01-01", used=True), "removable": False}]
        with mock.patch.object(driver_actions, "is_elevated", return_value=True), \
                mock.patch.object(driver_actions.driver_store, "packages", return_value={"packages": pk}):
            with self.assertRaises(driver_actions.DriverActionError):
                driver_actions.remove_old_packages(["oem2.inf"])  # in use
            done = driver_actions.remove_old_packages(["OEM1.INF"])
        self.assertTrue(done["result"].startswith("simulated"))
        self.assertIn("5 MB", done["result"])

    def test_backup_goes_to_a_drive_of_this_pc(self):
        pk = [package("oem1.inf", "x.inf", "1", "2020-01-01", used=True, size=2 ** 30)]
        drives = [{"drive": "D:", "free_bytes": 300 * 2 ** 30, "system": False, "fits": True}]
        with mock.patch.object(driver_actions, "is_elevated", return_value=True), \
                mock.patch.object(driver_actions.driver_store, "packages", return_value={"packages": pk}), \
                mock.patch.object(driver_actions, "backup_drives", return_value=drives):
            with self.assertRaises(driver_actions.DriverActionError):
                driver_actions.backup("Z:", None)
            done = driver_actions.backup("d:", None)
        self.assertTrue(done["result"].startswith("simulated"))
        self.assertIn("D:\\OptiMind driver backup\\", done["path"])

    def test_restore_only_from_a_known_backup(self):
        with mock.patch.object(driver_actions, "is_elevated", return_value=True), \
                mock.patch.object(driver_actions, "backups", return_value=[{"path": "D:\\OptiMind driver backup\\2026-10-19 10.00"}]):
            with self.assertRaises(driver_actions.DriverActionError) as e:
                driver_actions.restore("C:\\Windows", None)
            self.assertEqual(e.exception.status, 404)
            self.assertTrue(driver_actions.restore("d:\\optimind driver backup\\2026-10-19 10.00", None)["result"]
                            .startswith("simulated"))

    def test_backup_drives_prefer_another_drive(self):
        parts = [mock.Mock(mountpoint="C:\\", opts="rw,fixed"), mock.Mock(mountpoint="D:\\", opts="rw,fixed"),
                 mock.Mock(mountpoint="E:\\", opts="rw,removable")]
        free = {"C:\\": 600 * 2 ** 30, "D:\\": 300 * 2 ** 30}
        with mock.patch.object(driver_actions.psutil, "disk_partitions", return_value=parts), \
                mock.patch.object(driver_actions.psutil, "disk_usage", side_effect=lambda m: mock.Mock(free=free[m])), \
                mock.patch.dict("os.environ", {"SystemDrive": "C:"}):
            drives = driver_actions.backup_drives(3 * 2 ** 30)
        self.assertEqual([d["drive"] for d in drives], ["D:", "C:"])


class JobTests(unittest.TestCase):
    def wait(self):
        for _ in range(200):
            if not driver_jobs.busy():
                return driver_jobs.status()
            time.sleep(0.01)
        self.fail("the task didn't end")

    def test_one_task_at_a_time(self):
        go = threading.Event()
        ended = []

        def slow(job):
            job.update("Working…", 0.5)
            go.wait(5)
            return {"result": "done it"}

        status = driver_jobs.start("t", "A task", slow, on_end=ended.append)
        self.assertEqual(status["state"], "running")
        with self.assertRaises(driver_jobs.JobBusy):
            driver_jobs.start("t2", "Another", lambda job: {})
        self.assertEqual(driver_jobs.status()["progress"], 0.5)
        go.set()
        final = self.wait()
        self.assertEqual((final["state"], final["result"]["result"]), ("done", "done it"))
        self.assertEqual(len(ended), 1)

    def test_a_failure_is_kept(self):
        def broken(job):
            raise driver_actions.DriverActionError("no space on D:")

        driver_jobs.start("t", "Backing up", broken)
        final = self.wait()
        self.assertEqual((final["state"], final["error"]), ("failed", "no space on D:"))


# ------------------------------------------------------------ the API

def fake_report(**changes):
    report = {"checked_at": "2026-10-19T10:00:00+00:00", "findings": [], "devices": [], "key_drivers": [],
              "packages": [], "software": [], "events": [], "summary": {"devices": 0}, "elevated": False, "dry_run": True,
              "updates": {"windows_update": {"updates": [{"id": "b2b1a9c1-0d5c-4c83-9e6a-0123456789ab", "title": "Old",
                                                          "verdict": "older"}]}, "nvidia": None}, "job": None}
    report.update(changes)
    return report


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = api_client(app)  # no loops: only these endpoints

    def test_report_and_summary(self):
        with mock.patch.object(drivers, "report", return_value=fake_report()):
            self.assertEqual(self.client.get("/api/drivers").json()["checked_at"], "2026-10-19T10:00:00+00:00")
        with mock.patch.object(drivers, "summary", return_value={"checked_at": None, "findings": []}):
            self.assertIn("findings", self.client.get("/api/drivers/summary").json())
        self.assertIn("job", self.client.get("/api/drivers/job").json())

    def test_summary_never_waits_for_a_scan(self):
        started = threading.Event()
        with mock.patch.object(drivers, "_cache", None), \
                mock.patch.object(drivers, "_quiet_scan", side_effect=lambda: started.set()):
            began = time.perf_counter()
            out = drivers.summary()
            self.assertLess(time.perf_counter() - began, 0.5)
            self.assertTrue(started.wait(2))  # a scan was started in the background instead
        self.assertEqual(out, {"checked_at": None, "findings": []})

    def test_changes_are_refused_without_rights_and_logged(self):
        from app.service.state import app_state

        with mock.patch("app.tools.driver_actions.is_elevated", return_value=False):
            r = self.client.post("/api/drivers/scan")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(app_state.recent_actions[-1].action, ActionType.SCAN_DEVICES)
        self.assertFalse(app_state.recent_actions[-1].applied)
        for path, body in (("/api/drivers/windows-update/install", {"ids": ["b2b1a9c1-0d5c-4c83-9e6a-0123456789ab"]}),
                           ("/api/drivers/nvidia/install", None), ("/api/drivers/backup", {"drive": "D:"}),
                           ("/api/drivers/packages/remove", {"names": ["oem1.inf"]}),
                           ("/api/drivers/backups/restore", {"path": "D:\\OptiMind driver backup\\x"})):
            with self.subTest(path), mock.patch("app.api.drivers_routes.is_elevated", return_value=False):
                self.assertEqual(self.client.post(path, json=body).status_code, 403)

    def test_never_an_older_driver_or_during_a_game(self):
        with mock.patch("app.api.drivers_routes.is_elevated", return_value=True), \
                mock.patch.object(drivers, "report", return_value=fake_report()):
            with mock.patch("app.api.drivers_routes.games.status", return_value={"current": None}):
                r = self.client.post("/api/drivers/windows-update/install", json={"ids": ["b2b1a9c1-0d5c-4c83-9e6a-0123456789ab"]})
                self.assertEqual(r.status_code, 409)
                self.assertIn("older than the driver you have", r.json()["detail"])
            with mock.patch("app.api.drivers_routes.games.status", return_value={"current": {"game": "HELLDIVERS 2"}}):
                r = self.client.post("/api/drivers/windows-update/install", json={"ids": ["b2b1a9c1-0d5c-4c83-9e6a-0123456789ab"]})
                self.assertEqual(r.status_code, 409)
                self.assertIn("game is running", r.json()["detail"])

    def test_never_firmware(self):
        report = fake_report(updates={"windows_update": {"updates": [
            {"id": "b2b1a9c1-0d5c-4c83-9e6a-0123456789ab", "title": "ASUSTeK COMPUTER INC. - Firmware - 4.6.31",
             "verdict": "firmware"}]}, "nvidia": None})
        with mock.patch("app.api.drivers_routes.is_elevated", return_value=True), \
                mock.patch("app.api.drivers_routes.games.status", return_value={"current": None}), \
                mock.patch.object(drivers, "report", return_value=report), \
                mock.patch.object(driver_updates, "install_windows_update", side_effect=AssertionError("nothing installs")):
            r = self.client.post("/api/drivers/windows-update/install", json={"ids": ["b2b1a9c1-0d5c-4c83-9e6a-0123456789ab"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("never installs firmware or BIOS updates", r.json()["detail"])

    def test_input_is_checked(self):
        for path, body in (("/api/drivers/windows-update/install", {"ids": ["'; drop"]}),
                           ("/api/drivers/windows-update/install", {"ids": []}),
                           ("/api/drivers/packages/remove", {"names": ["..\\..\\x.inf"]}),
                           ("/api/drivers/backup", {"drive": "D:\\Windows"}),
                           ("/api/drivers/devices/restart", {"id": "x", "extra": 1})):
            with self.subTest(body):
                self.assertEqual(self.client.post(path, json=body).status_code, 422)

    def test_a_task_runs_in_the_background(self):
        with mock.patch.object(driver_updates, "search_windows_update",
                               return_value={"updates": [{"id": "x"}], "checked_at": "2026-10-19T10:00:00+00:00"}):
            r = self.client.post("/api/drivers/windows-update/check")
            self.assertEqual(r.status_code, 200)
            for _ in range(200):
                job = self.client.get("/api/drivers/job").json()["job"]
                if job["state"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual((job["state"], job["result"]["result"]), ("done", "Windows Update offers 1 driver update"))


if __name__ == "__main__":
    unittest.main()
