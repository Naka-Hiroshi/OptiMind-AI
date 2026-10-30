"""OptiMind on any PC or laptop: what works on this PC (and why not), laptops without a High
performance plan, AMD and Intel graphics, Intel's 13th/14th gen fix and the BIOS menus of each
board maker. The hardware is faked; nothing on the PC is read or changed unless it says so."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from app.system import capabilities, hardware
from tests import api_client

ON_WINDOWS = sys.platform == "win32"


def item(items, id):
    return next(i for i in items if i["id"] == id)


class CapabilitiesTests(unittest.TestCase):
    def build(self, *, elevated=True, can_ask=True, presentmon=True, measure=True, nvidia=None, adapters=(),
              plans=("High performance", "Balanced"), battery=None, classes=(0, 0, 0, 0)):
        sets = [SimpleNamespace(EfficiencyClass=c) for c in classes]
        with mock.patch("app.system.admin_rights.is_elevated", return_value=elevated), \
                mock.patch("app.system.admin_rights.can_ask_for_admin", return_value=can_ask), \
                mock.patch("app.system.frames.available", return_value=presentmon), \
                mock.patch.object(capabilities.settings, "optimind_measure_games", measure), \
                mock.patch("app.system.gpu.available", return_value=nvidia is not None), \
                mock.patch("app.system.gpu.primary", return_value={"name": nvidia} if nvidia else None), \
                mock.patch("app.system.hardware.display_adapters", return_value=list(adapters)), \
                mock.patch("app.system.monitor.installed_power_plans", return_value=None if plans is None else list(plans)), \
                mock.patch.object(capabilities.psutil, "sensors_battery", return_value=battery), \
                mock.patch("app.agent.optimizer.cpu_sets", return_value=sets), \
                mock.patch.object(capabilities, "IS_WINDOWS", True):
            return capabilities.report()

    def test_a_gaming_desktop_with_nvidia(self):
        items = self.build(nvidia="NVIDIA GeForce RTX 2060 SUPER", adapters=["NVIDIA GeForce RTX 2060 SUPER"])
        self.assertEqual([i["status"] for i in items], ["on"] * 6)
        self.assertIn("RTX 2060 SUPER", item(items, "gpu_sensors")["text"])

    def test_a_laptop_with_intel_graphics_and_only_balanced(self):
        items = self.build(adapters=["Intel(R) Iris(R) Xe Graphics"], plans=["Balanced"], battery=object(),
                           classes=(0, 0, 1, 1))
        plan = item(items, "power_plan")
        self.assertEqual(plan["status"], "partial")
        self.assertIn("Best performance while plugged in", plan["text"])
        self.assertEqual(plan["link"], "ms-settings:powersleep")
        self.assertIn("efficiency cores", item(items, "cores")["text"])  # Intel's performance + efficiency cores
        self.assertIn("Intel's graphics app", item(items, "gpu_sensors")["text"])
        self.assertIn("Intel Driver & Support Assistant", item(items, "newest_driver")["text"])

    def test_amd_graphics(self):
        items = self.build(adapters=["AMD Radeon RX 6600"])
        self.assertEqual(item(items, "gpu_sensors")["status"], "partial")
        self.assertIn("AMD Software shows them", item(items, "gpu_sensors")["text"])
        self.assertIn("amd.com", item(items, "newest_driver")["link"])

    def test_without_administrator_rights(self):
        items = self.build(elevated=False)
        self.assertEqual(item(items, "admin")["status"], "partial")
        self.assertIn("Restart with administrator rights", item(items, "admin")["action"])
        self.assertEqual(item(items, "frames")["status"], "off")
        self.assertIn("isn't an administrator", item(self.build(elevated=False, can_ask=False), "admin")["action"])

    def test_frame_rates(self):
        self.assertEqual(item(self.build(presentmon=False), "frames")["status"], "off")
        self.assertEqual(item(self.build(measure=False), "frames")["status"], "partial")  # turned off in Settings

    def test_a_desktop_with_high_performance(self):
        text = item(self.build(battery=None), "power_plan")["text"]
        self.assertTrue(text.startswith("OptiMind switches to High performance") and "battery" not in text)
        self.assertIn("never on battery", item(self.build(battery=object()), "power_plan")["text"])

    def test_what_it_cant_find_out_is_left_out(self):
        self.assertNotIn("power_plan", [i["id"] for i in self.build(plans=None)])
        with mock.patch.object(capabilities, "_cores", side_effect=RuntimeError("no")):
            self.assertNotIn("cores", [i["id"] for i in self.build()])  # one failing item doesn't stop the rest

    def test_the_card_that_matters_most(self):
        self.assertEqual(capabilities._vendor(["Intel(R) UHD Graphics 630", "NVIDIA GeForce RTX 4060 Laptop GPU"]),
                         "nvidia")  # a laptop's own graphics next to its gaming card
        self.assertEqual(capabilities._vendor(["AMD Radeon(TM) Graphics"]), "amd")
        self.assertEqual(capabilities._vendor([]), "")

    def test_the_api(self):
        from app.main import app

        with mock.patch.object(capabilities, "report", return_value=[{"id": "admin"}]):
            self.assertEqual(api_client(app).get("/api/app/capabilities").json(), {"items": [{"id": "admin"}]})


class LaptopPowerPlanTests(unittest.TestCase):
    def test_a_plan_this_pc_doesnt_have_is_never_proposed(self):
        from app.agent.decision_engine import DecisionEngine
        from app.models import OptimizationPolicy
        from tests.test_decision_engine import snap

        policy = OptimizationPolicy(enable_page_combining=False)
        for plans, expected in ((["Balanced"], None), (["High performance", "Balanced"], "High performance")):
            with self.subTest(plans=plans):
                e = DecisionEngine()
                busy = [e._power_plan_action(snap(cpu=95, plan="Balanced").model_copy(update={"power_plans": plans}),
                                             policy, t) for t in (1000, 1002, 1004)]
                self.assertEqual(busy[-1] and busy[-1].target, expected)
                playing = DecisionEngine()._power_plan_action(
                    snap(cpu=30, plan="Balanced").model_copy(update={"power_plans": plans}), policy, 1000, game=True)
                self.assertEqual(playing and playing.target, expected)

    @unittest.skipUnless(ON_WINDOWS, "Windows only")
    def test_this_pcs_plans(self):
        from app.system import monitor

        self.assertIn("Balanced", monitor.installed_power_plans())  # read from this PC (every PC has Balanced)


class HardwareCheckTests(unittest.TestCase):
    def test_each_board_makers_bios_menus(self):
        for maker, name, memory_menu in (("ASUSTeK COMPUTER INC.", "asus", "Ai Overclock Tuner"),
                                         ("Micro-Star International Co., Ltd.", "msi", "A-XMP"),
                                         ("Gigabyte Technology Co., Ltd.", "gigabyte", "Extreme Memory Profile"),
                                         ("ASRock", "asrock", "OC Tweaker"), ("LENOVO", "", None)):
            with self.subTest(maker), mock.patch.object(hardware, "_wmi",
                                                        return_value=[SimpleNamespace(Manufacturer=maker)]):
                self.assertEqual(hardware._board_maker(), name)
            if name:
                self.assertIn(memory_menu, hardware._MEMORY_PROFILE_MENU[name])
                self.assertIn("Overclocking" if name != "gigabyte" else "Advanced CPU Settings", hardware._PBO_MENU[name])

    def test_amd_graphics_card_tuning(self):
        with mock.patch("app.system.gpu.primary", return_value=None), \
                mock.patch.object(hardware, "display_adapters", return_value=["AMD Radeon RX 7800 XT"]), \
                mock.patch.object(hardware, "_recent_limits", return_value={"gpu": 2, "cpu": 0}):
            check = hardware._gpu_tuning_check()
        self.assertEqual((check["id"], check["status"]), ("gpu_tuning", "tip"))
        self.assertIn("Undervolt GPU", check["action"])
        with mock.patch("app.system.gpu.primary", return_value=None), \
                mock.patch.object(hardware, "display_adapters", return_value=["AMD Radeon(TM) Graphics"]):
            self.assertIsNone(hardware._gpu_tuning_check())  # built-in graphics: nothing to tune there

    def intel(self, cpu, revision_bytes):
        values = {"ProcessorNameString": cpu, "Update Revision": revision_bytes}
        with mock.patch.object(hardware, "_reg", side_effect=lambda path, name, hive=None: values.get(name)):
            return hardware._intel_stability_check()

    def test_intel_13th_14th_gen(self):
        old = bytes([0, 0, 0, 0, 0x29, 0x01, 0, 0])  # Intel keeps the version in the upper half: 0x129
        check = self.intel("13th Gen Intel(R) Core(TM) i9-13900K", old)
        self.assertEqual(check["status"], "warn")
        self.assertIn("microcode 0x129", check["summary"])
        self.assertIn("0x12F", check["action"])
        self.assertEqual(self.intel("Intel(R) Core(TM) i7-14700K", bytes([0, 0, 0, 0, 0x2B, 1, 0, 0]))["status"], "tip")
        self.assertEqual(self.intel("Intel(R) Core(TM) i7-14700K", bytes([0, 0, 0, 0, 0x2F, 1, 0, 0]))["status"], "good")
        self.assertEqual(self.intel("13th Gen Intel(R) Core(TM) i5-13400F", old)["status"], "info")  # Intel: none reported
        self.assertEqual(self.intel("Intel(R) Core(TM) i9-14900K", None)["status"], "info")  # version unknown

    def test_not_these(self):
        for cpu in ("13th Gen Intel(R) Core(TM) i7-13700H", "Intel(R) Core(TM) i9-14900HX",  # laptops
                    "12th Gen Intel(R) Core(TM) i7-12700K", "AMD Ryzen 5 3400G with Radeon Vega Graphics"):
            with self.subTest(cpu):
                self.assertIsNone(self.intel(cpu, bytes(8)))

    def test_amds_layout_of_the_version_too(self):
        with mock.patch.object(hardware, "_reg", return_value=bytes([0x09, 0x81, 0x10, 0x08])):
            self.assertEqual(hardware._microcode_revision(), 0x08108109)  # this PC's Ryzen: 4 bytes


if __name__ == "__main__":
    unittest.main()
