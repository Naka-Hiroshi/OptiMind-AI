"""The makers' own driver apps (the Drivers page): which fit a PC (its graphics, its Intel
parts, its maker), whether each is in the Start menu, and that the window opens only an app
the agent lists. The PCs are made up and nothing is started; on Windows, one test reads this
PC's Start menu (read-only)."""
import io
import json
import re
import sys
import unittest
from unittest import mock

from app.desktop import window
from app.system import drivers, maker_tools
from tests import api_client

ON_WINDOWS = sys.platform == "win32"


def device(cls, instance, present=True):
    return {"class": cls, "id": instance, "hardware_id": None, "present": present}


NVIDIA = device("Display", "PCI\\VEN_10DE&DEV_1F06\\4&1")
RADEON = device("Display", "PCI\\VEN_1002&DEV_73BF\\4&2")
INTEL_GPU = device("Display", "PCI\\VEN_8086&DEV_A7A0\\3&1")
INTEL_BLUETOOTH = device("Bluetooth", "USB\\VID_8087&PID_0033\\5&1")
AMD_CHIPSET = device("System", "PCI\\VEN_1022&DEV_1480\\3&2")
APPS = [{"name": "NVIDIA App", "app_id": "com.nvidia.nvapp"}, {"name": "SteelSeries GG", "app_id": "SteelSeries.Gg.Main"}]


def firmware(system_maker, board_maker=""):
    return {"system": {"maker": system_maker, "model": ""}, "board": {"maker": board_maker, "model": ""},
            "support": drivers.maker_support(system_maker or board_maker)}


class WhichAppsTests(unittest.TestCase):
    def tools(self, devices, fw, apps=()):
        return {t["id"]: t for t in maker_tools.tools(devices, fw, list(apps))}

    def test_a_self_built_pc_like_this_one(self):
        # NVIDIA graphics, an AMD chipset, an ASUS board: NVIDIA App, and ASUS's app only if it's installed
        got = self.tools([NVIDIA, AMD_CHIPSET], firmware("", "ASUSTeK COMPUTER INC."), APPS)
        self.assertEqual(list(got), ["nvidia_app"])
        self.assertEqual((got["nvidia_app"]["installed"], got["nvidia_app"]["app_id"], got["nvidia_app"]["start_name"]),
                         (True, "com.nvidia.nvapp", "NVIDIA App"))
        crate = {"name": "Armoury Crate", "app_id": "B9ECED6F.ArmouryCrate_qmba6cd70vzyy!App"}
        self.assertTrue(self.tools([NVIDIA], firmware("", "ASUSTeK COMPUTER INC."), APPS + [crate])["armoury_crate"]
                        ["installed"])

    def test_a_dell_laptop_with_intel_graphics(self):
        got = self.tools([INTEL_GPU, INTEL_BLUETOOTH], firmware("Dell Inc.", "Dell Inc."))
        self.assertEqual(sorted(got), ["dell", "intel_dsa"])
        self.assertFalse(got["dell"]["installed"])
        self.assertEqual((got["dell"]["url"], got["dell"]["url_label"]),
                         ("https://www.dell.com/support/home", "Dell Drivers & Downloads"))  # the maker's own page
        self.assertEqual(got["intel_dsa"]["url"], "https://www.intel.com/content/www/us/en/support/detect.html")

    def test_brands_and_parts(self):
        for maker, tool in (("HP", "hp"), ("LENOVO", "lenovo"), ("ASUSTeK COMPUTER INC.", "myasus"), ("Alienware", "dell"),
                            ("Micro-Star International Co., Ltd.", "msi_center"), ("Acer", "acer"),
                            ("Gigabyte Technology Co., Ltd.", "gigabyte_control_center")):
            with self.subTest(maker):
                self.assertIn(tool, self.tools([RADEON], firmware(maker)))
        self.assertIn("amd_software", self.tools([RADEON], firmware("HP")))
        self.assertEqual(self.tools([device("Display", "PCI\\VEN_10DE&DEV_1F06\\4&9", present=False)], {}), {})
        self.assertNotIn("intel_dsa", self.tools([AMD_CHIPSET], {}))  # an AMD chipset isn't Intel's

    def test_found_by_their_names_in_the_start_menu(self):
        names = {"nvidia_app": ["NVIDIA App", "GeForce Experience"], "amd_software": ["AMD Software: Adrenalin Edition"],
                 "intel_dsa": ["Intel® Driver & Support Assistant", "Intel Driver and Support Assistant"],
                 "dell": ["Dell SupportAssist", "SupportAssist", "Dell Command | Update"], "hp": ["HP Support Assistant"],
                 "lenovo": ["Lenovo Vantage", "Lenovo Commercial Vantage", "Lenovo System Update"], "myasus": ["MyASUS"],
                 "armoury_crate": ["Armoury Crate"], "msi_center": ["MSI Center"],
                 "gigabyte_control_center": ["GIGABYTE Control Center"], "acer": ["Acer Care Center", "AcerSense"]}
        for tool in maker_tools.TOOLS:
            for name in names[tool.id]:
                with self.subTest(name):
                    self.assertTrue(re.search(tool.match, name, re.IGNORECASE))
            for other in ("SteelSeries GG", "Microsoft Edge", "Windows Update", "HP Smart"):
                self.assertFalse(re.search(tool.match, other, re.IGNORECASE), f"{tool.id} matched {other}")


class StartMenuTests(unittest.TestCase):
    def test_reading_get_start_apps(self):
        self.assertEqual(maker_tools.parse_start_apps('{"Name":"NVIDIA App","AppID":"com.nvidia.nvapp"}'),
                         [{"name": "NVIDIA App", "app_id": "com.nvidia.nvapp"}])  # one app: an object, not a list
        two = json.dumps([{"Name": "A", "AppID": "a"}, {"Name": "B", "AppID": "b"}, {"Name": "", "AppID": "c"}])
        self.assertEqual(len(maker_tools.parse_start_apps(two)), 2)
        self.assertEqual(maker_tools.parse_start_apps(""), [])
        self.assertEqual(maker_tools.parse_start_apps("not json"), [])

    def test_a_failed_read_is_asked_again(self):
        failed = mock.Mock(returncode=1, stdout=b"")
        with mock.patch.object(maker_tools, "IS_WINDOWS", True), mock.patch.object(maker_tools, "_apps", None), \
                mock.patch.object(maker_tools.subprocess, "run", return_value=failed) as run:
            self.assertEqual(maker_tools.start_apps(), [])
            maker_tools.start_apps()
        self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_this_pcs_start_menu(self):
        with mock.patch.object(maker_tools, "_apps", None):  # read-only: Get-StartApps
            apps = maker_tools.start_apps()
        self.assertTrue(apps)
        self.assertTrue(all(a["name"] and a["app_id"] for a in apps))


class OpenTests(unittest.TestCase):
    def listed(self, tools):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps({"tools": tools}).encode())
        return mock.patch.object(window.urllib.request, "urlopen", return_value=response)

    def test_only_an_app_the_agent_lists(self):
        tools = [{"id": "nvidia_app", "installed": True, "app_id": "com.nvidia.nvapp"},
                 {"id": "dell", "installed": False, "app_id": None}]
        with self.listed(tools), mock.patch("subprocess.Popen") as popen:
            self.assertTrue(window.open_maker_app("com.nvidia.nvapp"))
            popen.assert_called_once_with(["explorer.exe", "shell:AppsFolder\\com.nvidia.nvapp"])
            for other in ("Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "", "None", 'com.nvidia.nvapp" & calc'):
                with self.subTest(other):
                    self.assertFalse(window.open_maker_app(other))
        self.assertEqual(popen.call_count, 1)

    def test_nothing_opens_when_the_agent_doesnt_answer(self):
        with mock.patch.object(window.urllib.request, "urlopen", side_effect=OSError("refused")), \
                mock.patch("subprocess.Popen") as popen:
            self.assertFalse(window.open_maker_app("com.nvidia.nvapp"))
        popen.assert_not_called()

    def test_the_api(self):
        from app.main import app

        client = api_client(app)
        with mock.patch.object(drivers, "_cache", (0.0, {"tools": [{"id": "nvidia_app"}]}, {})):
            self.assertEqual(client.get("/api/drivers/tools").json(), {"tools": [{"id": "nvidia_app"}]})
        with mock.patch.object(drivers, "_cache", None):
            self.assertEqual(client.get("/api/drivers/tools").json(), {"tools": []})


if __name__ == "__main__":
    unittest.main()
