"""Games and hardware (1.2): frame-time analysis, game sessions and their advice, the
stability watch, drive health, the DirectX-flag and disk tweaks, and GPU priority.
Nothing on the PC is changed: Windows calls are faked, except GPU priority, which is
tried on a throwaway process these tests start themselves."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import psutil

from app.agent import games, optimizer
from app.agent.decision_engine import DecisionEngine
from app.agent.safety import SafetyViolation, validate_action
from app.models import ActionType, OptimizationAction, OptimizationPolicy, ProcessSample, SystemSnapshot
from app.system import frames, hardware, processes, stability
from app.tools import tweaks

ON_WINDOWS = sys.platform == "win32"

# PresentMon 2.x metrics, as `--v2_metrics --no_track_input` writes them
V2 = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,"
      "CPUStartQPC,FrameTime,CPUBusy,CPUWait,GPULatency,GPUTime,GPUBusy,GPUWait,DisplayLatency,DisplayedTime,"
      "AnimationError,AnimationTime,MsFlipDelay")


def row(frame, cpu_busy, gpu_busy, pid=100, chain="0x1A", sync=0, mode="Hardware: Independent Flip", shown=True):
    gpu = "NA" if gpu_busy is None else f"{gpu_busy:.4f}"
    return (f"game.exe,{pid},{chain},DXGI,{sync},0,{0 if sync else 1},{mode},1000,{frame:.4f},{cpu_busy:.4f},"
            f"{max(0.0, frame - cpu_busy):.4f},0.1000,{gpu},{gpu},0.0,20.0,{f'{frame:.4f}' if shown else 'NA'},NA,1.0,NA")


def csv(rows):
    return [V2, *rows]


def wobble(i, base, spread):
    """Deterministic uneven frame times: base ± spread."""
    return base + spread * ((i * 37 % 17) / 8 - 1)


class FrameParsingTests(unittest.TestCase):
    def test_v2_columns(self):
        frames_ = frames.parse(csv([row(16.6, 8.0, 15.0), row(16.8, 8.2, 15.1, shown=False)]))
        self.assertEqual(len(frames_), 2)
        f = frames_[0]
        self.assertEqual((f.frame_ms, f.cpu_busy_ms, f.gpu_busy_ms, f.pid, f.swapchain), (16.6, 8.0, 15.0, 100, "0x1A"))
        self.assertTrue(f.displayed)
        self.assertFalse(frames_[1].displayed)  # NA: never on screen

    def test_default_columns_too(self):
        header = "Application,ProcessID,SwapChainAddress,SyncInterval,PresentMode,MsBetweenAppStart,MsCPUBusy,MsGPUBusy"
        frames_ = frames.parse([header, "game.exe,7,0x1,0,Composed: Flip,10.0000,9.0000,2.0000"])
        self.assertEqual((frames_[0].frame_ms, frames_[0].cpu_busy_ms, frames_[0].gpu_busy_ms), (10.0, 9.0, 2.0))

    def test_only_the_game_process(self):
        frames_ = frames.parse(csv([row(10, 5, 5, pid=1), row(10, 5, 5, pid=2), row(10, 5, 5, pid=2)]), pid=2)
        self.assertEqual(len(frames_), 2)

    def test_missing_frame_time_column_is_an_error(self):
        with self.assertRaises(frames.FramesError):
            frames.parse(["Application,ProcessID", "game.exe,1"])

    def test_a_browser_engine_game_presents_from_a_helper_process(self):
        # An Electron / NW.js game: the window's process presents a few frames, its GPU process all the others
        rows = [row(16.7, 1, 15, pid=2, chain="0xA") for _ in range(5)] + \
               [row(16.7, 8, 15, pid=3, chain="0xA") for _ in range(200)] + [row(16.7, 8, 15, pid=9) for _ in range(50)]
        parsed = frames.parse(csv(rows), pids={2, 3})
        self.assertEqual(len(parsed), 205)  # another program (pid 9) isn't the game's
        self.assertEqual(frames.analyze(parsed)["frames"], 200)  # the helper that presents (same chain address, pid 3)

    def test_capture_follows_the_game_and_its_helpers_by_name(self):
        exe = Path("PresentMon.exe")
        by_name = frames.Capture(pid=10, names=["Game.exe", "Game.exe", "msedgewebview2.exe", ""], pids={10, 11})
        command = by_name.command(exe)
        self.assertNotIn("--process_id", command)
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "--process_name"],
                         ["Game.exe", "msedgewebview2.exe"])
        self.assertEqual(frames.Capture(pid=10).command(exe)[1:3], ["--process_id", "10"])
        self.assertIsNone(frames.Capture(pid=10, pids={10}).pids)  # no names: the window's process only


class FrameAnalysisTests(unittest.TestCase):
    def analyze(self, rows, refresh=None):
        return frames.analyze(frames.parse(csv(rows)), refresh)

    def test_gpu_limited(self):
        r = self.analyze([row(f := wobble(i, 16.7, 1.0), f * 0.5, f * 0.96) for i in range(300)], refresh=165)
        self.assertEqual(r["limiter"], "gpu")
        self.assertGreater(r["shares"]["gpu"], 0.99)
        self.assertAlmostEqual(r["fps"], 60, delta=2)

    def test_cpu_limited(self):
        r = self.analyze([row(f := wobble(i, 15.0, 5.0), f * 0.97, 4.0) for i in range(300)], refresh=165)
        self.assertEqual(r["limiter"], "cpu")
        self.assertIsNone(r["cap"])

    def test_vsync_is_a_cap_not_the_cpu(self):
        # Like the desktop compositor in PresentMon's own trace: it waits for the refresh inside its frame
        r = self.analyze([row(6.06 + 0.01 * (i % 3), 5.9, 2.0, sync=1) for i in range(400)], refresh=165)
        self.assertEqual(r["limiter"], "cap")
        self.assertEqual(r["cap"], {"kind": "vsync", "fps": 165})

    def test_a_frame_limiter_is_a_cap(self):
        r = self.analyze([row(7.092, 3.0, 3.0) for _ in range(400)], refresh=144)
        self.assertEqual((r["limiter"], r["cap"]["kind"], r["cap"]["fps"]), ("cap", "cap", 141))

    def test_steady_but_odd_frame_rate_with_a_busy_cpu_is_the_cpu(self):
        # 77 fps: no cap sets that, so steady frames with a busy CPU are the processor's limit. (Frame
        # times alone can't tell a CPU that happens to hold a cap's exact rate, e.g. 72 fps, from a cap.)
        r = self.analyze([row(12.9 + 0.2 * (i % 2), 12.6, 4.0) for i in range(300)], refresh=165)
        self.assertEqual(r["limiter"], "cpu")

    def test_no_gpu_timing(self):
        r = self.analyze([row(wobble(i, 12.0, 4.0), 6.0, None) for i in range(200)])
        self.assertEqual((r["limiter"], r["shares"]), ("unknown", {}))

    def test_lows_and_stutter(self):
        r = self.analyze([row(10.0, 5.0, 9.6) for _ in range(294)] + [row(50.0, 45.0, 9.6) for _ in range(6)])
        self.assertAlmostEqual(r["stutter_share"], 0.02, places=3)
        self.assertEqual(r["low_1_fps"], 20.0)
        self.assertEqual(r["frame_ms"]["max"], 50.0)

    def test_the_main_swap_chain_only(self):
        r = self.analyze([row(10, 5, 9.5, chain="0xA") for _ in range(200)] + [row(33, 1, 1, chain="0xB") for _ in range(40)])
        self.assertEqual(r["frames"], 200)

    def test_too_few_frames(self):
        self.assertIsNone(self.analyze([row(10, 5, 5) for _ in range(10)]))


@unittest.skipUnless(ON_WINDOWS and frames.executable() and os.environ.get("OPTIMIND_TEST_ETL"),
                     "needs tools/PresentMon.exe and OPTIMIND_TEST_ETL (a PresentMon test trace)")
class RealPresentMonTests(unittest.TestCase):
    def test_a_recorded_trace(self):
        capture = frames.Capture(etl_file=os.environ["OPTIMIND_TEST_ETL"], refresh_hz=60).start()
        self.assertTrue(capture.done.wait(60))
        self.assertIsNone(capture.error)
        self.assertGreater(capture.result["frames"], 30)
        self.assertGreater(capture.result["fps"], 0)


STEAM_GAME = {"name": "Test Game", "store": "Steam", "appid": "123", "gb": 40.0, "played": 0,
              "path": r"D:\SteamLibrary\steamapps", "install_path": r"D:\SteamLibrary\steamapps\common\TestGame"}
DRIVES = [{"letter": "C:", "kind": "NVMe SSD", "model": "", "free_gb": 600.0, "total_gb": 930.0, "percent": 35},
          {"letter": "D:", "kind": "hard disk", "model": "", "free_gb": 300.0, "total_gb": 930.0, "percent": 67}]
GPU = {"name": "NVIDIA GeForce RTX 2060 SUPER", "utilization": 97, "temperature_c": 72, "slowdown_c": 93,
       "clock_mhz": 1905, "max_clock_mhz": 2115, "power_w": 172.0, "power_limit_w": 175.0, "max_power_limit_w": 200.0,
       "memory_used_mb": 6100, "memory_total_mb": 8192, "reasons": ["power"]}


class SessionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = (os.getpid(), "game.exe", r"D:\SteamLibrary\steamapps\common\TestGame\bin\game.exe")
        self.alive = True
        for target, value in (
            ("SESSIONS_PATH", Path(folder.name) / "game_sessions.jsonl"), ("_session", None), ("_saved", None),
            ("_capture", None), ("_capture_manual", False), ("_capture_focus_mode", None), ("_capture_mode_mixed", False),
            ("_last_tick", None), ("_frames_paused_until", 0.0), ("_next_capture", 0.0),
        ):
            patcher = mock.patch.object(games, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for obj, name, value in (
            (games.frames, "available", lambda: False), (games.gpu, "primary", lambda: GPU),
            (games.hardware, "installed_games_cached", lambda: [STEAM_GAME]), (games.hardware, "drives", lambda: DRIVES),
            (games.hardware, "drive_kind", lambda p: "hard disk" if p and p.upper().startswith("D:") else "NVMe SSD"),
            (games.display, "refresh_hz", lambda: 165.0), (games.focus, "current_app", lambda: self.app),
            (games, "_alive", lambda s: self.alive), (games, "is_elevated", lambda: True),
            (games.monitor, "latest_processes", lambda: ([], None)),
        ):
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def snapshot(self, game=True):
        me = ProcessSample(pid=os.getpid(), name="game.exe", cpu_percent=180.0, memory_mb=5200, io_bps=40 * 2 ** 20,
                           is_foreground=True)
        return SystemSnapshot(cpu_percent=60, ram_percent=71, ram_available_mb=4000, top_processes=[me],
                              foreground_game=game)

    def play(self, start, seconds, game=True):
        for t in range(start, start + seconds + 1, 2):
            games.update(self.snapshot(game), now=float(t))
        return start + seconds

    def test_a_session_from_start_to_end(self):
        t = self.play(1000, 120)
        live = games.status()["current"]
        self.assertEqual((live["game"], live["minutes"]), ("Test Game", 2.0))
        self.assertIn("isn't installed", live["frames_error"])  # tried to measure after 45 s
        self.alive = False
        games.update(self.snapshot(game=False), now=t + 2.0)  # the game closed
        [s] = games.sessions()
        self.assertEqual((s["game"], s["store"], s["appid"], s["drive"], s["minutes"]), ("Test Game", "Steam", "123", "hard disk", 2.0))
        self.assertEqual((s["gpu"]["temp_max"], s["gpu"]["reasons"]), (72, {"power": 1.0}))
        self.assertEqual(s["memory"]["max_percent"], 71)
        self.assertIsNone(s["frames"])
        ids = [a["id"] for a in s["advice"]]
        self.assertIn("move_to_ssd", ids)
        self.assertIn("C:", next(a for a in s["advice"] if a["id"] == "move_to_ssd")["text"])
        self.assertTrue(games.SESSIONS_PATH.exists())

    def test_homes_chart_gets_each_captures_frame_rate(self):
        self.play(1000, 10)
        capture = {"frames": 900, "seconds": 30.0, "fps": 30.0, "low_1_fps": 20.0, "stutter_share": 0.0, "shares": {},
                   "cap": None, "gpu_busy": None, "latency_ms": None, "frame_ms": {"median": 33.3}}
        games._session.captures = [{**capture, "fps": 30.0 + i, "at": f"t{i}"} for i in range(25)]
        live = games.status()["current"]
        self.assertEqual(len(live["history"]), 20)  # about the last hour of play
        self.assertEqual(live["history"][-1], {"at": "t24", "fps": 54.0, "low_1_fps": 20.0})  # and nothing else of it
        self.assertEqual(len(live["captures"]), 3)

    def test_alt_tab_does_not_end_it(self):
        t = self.play(1000, 60)
        for x in range(t + 2, t + 102, 2):  # 100 s in the background
            games.update(self.snapshot(game=False), now=float(x))
        t = self.play(t + 102, 40)
        self.assertEqual(games.status()["current"]["minutes"], round(100 / 60, 1))  # only time in front counts

    def test_five_minutes_away_ends_it(self):
        t = self.play(1000, 90)
        for x in range(t + 2, t + 310, 2):
            games.update(self.snapshot(game=False), now=float(x))
        self.assertIsNone(games.status()["current"])
        self.assertEqual(len(games.sessions()), 1)

    def test_background_recording_is_noticed(self):
        recorder = 4242
        rows = [{"pid": recorder, "name": "SteelSeriesCaptureSvc.exe"}, {"pid": os.getpid(), "name": "game.exe"}]
        with mock.patch.object(games.monitor, "encoders", lambda: {recorder: 19.0, os.getpid(): 1.0}), \
                mock.patch.object(games.monitor, "latest_processes", lambda: (rows, os.getpid())):
            t = self.play(1000, 90)
        self.alive = False
        games.update(self.snapshot(game=False), now=t + 2.0)
        [s] = games.sessions()
        self.assertEqual(s["recording"], [{"app": "SteelSeriesCaptureSvc.exe", "label": "SteelSeries GG Moments",
                                           "encode_avg": 19, "share": 1.0}])
        advice = next(a for a in s["advice"] if a["id"] == "recording")
        self.assertEqual(advice["title"], "SteelSeries GG Moments recorded the game")
        self.assertIn("auto-clipping", advice["text"])

    def test_memory_near_its_limit_is_noted_with_the_apps_holding_it(self):
        apps = [{"name": "Code.exe", "title": "Visual Studio Code", "mb": 2232}]

        def full():
            return self.snapshot().model_copy(update={"ram_percent": 93, "commit_used_mb": 24000, "commit_limit_mb": 25600,
                                                      "hard_faults_mb_s": 36.0})

        self.assertFalse(games.running())
        with mock.patch.object(games.processes, "biggest_apps", return_value=apps) as biggest:
            t = self.play(1000, 30)  # memory as usual
            self.assertTrue(games.running())
            for x in range(t + 2, t + 62, 2):
                games.update(full(), now=float(x))
        biggest.assert_called_once()  # when memory got fullest, not every cycle
        self.alive = False
        games.update(self.snapshot(game=False), now=t + 64.0)
        self.assertFalse(games.running())
        [s] = games.sessions()
        m = s["memory"]
        self.assertEqual((m["max_percent"], m["commit_max_percent"], m["commit_max_mb"], m["commit_limit_mb"]),
                         (93, 94, 24000, 25600))
        self.assertEqual((m["hard_faults_max_mbps"], m["apps"]), (36.0, apps))
        self.assertIn("commit_full", [a["id"] for a in s["advice"]])

    def finish(self, start=1000, seconds=90):
        t = self.play(start, seconds)
        self.alive = False
        games.update(self.snapshot(game=False), now=t + 2.0)
        self.alive = True
        return games.sessions()[0]

    def test_a_note_on_a_session(self):
        s = self.finish()
        self.assertEqual(games.set_note(s["started"], "GAME.EXE", "  render scale   Quality ")["note"], "render scale Quality")
        games._saved = None  # read again from the file: the note was saved with the session
        self.assertEqual(games.sessions()[0]["note"], "render scale Quality")
        self.assertEqual(games.recent_summary(1)[0]["note"], "render scale Quality")  # the AI chat sees it
        games.set_note(s["started"], "game.exe", "")
        self.assertNotIn("note", games.sessions()[0])
        self.assertEqual(len(games.set_note(s["started"], "game.exe", "x" * 500)["note"]), 120)
        with self.assertRaises(LookupError):
            games.set_note("2026-01-01T00:00:00+00:00", "game.exe", "no such session")

    def test_the_note_api(self):
        from app.main import app
        from tests import api_client

        s = self.finish()
        client = api_client(app)
        r = client.post("/api/games/note", json={"started": s["started"], "exe": s["exe"], "note": "browser closed"})
        self.assertEqual((r.status_code, r.json()["session"]["note"]), (200, "browser closed"))
        self.assertEqual(client.post("/api/games/note", json={"started": "x", "exe": "game.exe", "note": "a"}).status_code, 404)
        self.assertEqual(client.post("/api/games/note", json={"started": s["started"], "exe": s["exe"],
                                                              "note": "x" * 121}).status_code, 422)

    def test_before_you_play(self):
        rows = [{"pid": 10, "name": "Code.exe", "private_mb": 1200.0}, {"pid": 11, "name": "Code.exe", "private_mb": 1000.0},
                {"pid": 12, "name": "Discord.exe", "private_mb": 900.0}, {"pid": 13, "name": "small.exe", "private_mb": 60.0},
                {"pid": 14, "name": "SteelSeriesCaptureSvc.exe", "private_mb": 80.0},
                {"pid": os.getpid(), "name": "game.exe", "private_mb": 5000.0}]
        paths = {10: r"C:\Apps\Code.exe", 12: r"C:\Apps\Discord.exe", 13: r"C:\Apps\small.exe",
                 14: r"C:\Program Files\SteelSeries\SteelSeriesCaptureSvc.exe", os.getpid(): r"D:\Games\game.exe"}
        with mock.patch.object(games.monitor, "latest_processes", lambda: (rows, None)), \
                mock.patch.object(processes, "_exe_path", paths.get), \
                mock.patch.object(processes, "_title", lambda name, path: name[:-4]), \
                mock.patch.object(processes, "app_window_pids", lambda: {10, os.getpid()}), \
                mock.patch.object(games.monitor, "encoders", lambda: {14: 15.0}):
            self.finish()  # a session in which SteelSeries GG Moments recorded the game
            self.play(3000, 10)  # and the game is running again
            out = games.before_play()
        self.assertEqual(out["game"], "Test Game")
        self.assertEqual(out["apps"], [  # the game and anything below 150 MB left out; Discord has no window
            {"name": "Code.exe", "title": "Code", "mb": 2200, "processes": 2, "window": True},
            {"name": "Discord.exe", "title": "Discord", "mb": 900, "processes": 1, "window": False}])
        self.assertEqual(out["recorders"], [{"app": "SteelSeriesCaptureSvc.exe", "label": "SteelSeries GG Moments",
                                             "where": games.RECORDERS["steelseriescapturesvc.exe"][1]}])
        self.assertEqual(set(out["memory"]), {"total_mb", "available_mb", "percent", "commit_used_mb", "commit_limit_mb"})

    def test_a_recorder_is_named_before_it_runs_until_two_games_without_it(self):
        # Real PC: SteelSeries GG Moments' capture only runs while a game does, so before you played it wasn't named
        recorder = [{"pid": 14, "name": "SteelSeriesCaptureSvc.exe", "private_mb": 80.0},
                    {"pid": os.getpid(), "name": "game.exe", "private_mb": 5000.0}]
        with mock.patch.object(games.monitor, "latest_processes", lambda: (recorder, None)), \
                mock.patch.object(games.monitor, "encoders", lambda: {14: 15.0}):
            self.finish()  # it recorded this game
        with mock.patch.object(games.monitor, "latest_processes", lambda: ([], None)), \
                mock.patch.object(processes, "app_window_pids", lambda: set()):  # and doesn't run now
            named = lambda: [r["label"] for r in games.before_play()["recorders"]]  # noqa: E731
            self.assertEqual(named(), ["SteelSeries GG Moments"])
            self.finish(5000)  # a game it didn't record (you turned it off)
            self.assertEqual(named(), ["SteelSeries GG Moments"])
            self.finish(9000)  # and another one
            self.assertEqual(named(), [])

    def test_a_quick_look_is_not_saved(self):
        t = self.play(1000, 30)
        self.alive = False
        games.update(self.snapshot(game=False), now=t + 2.0)
        self.assertEqual(games.sessions(), [])

    def test_measure_now_needs_a_game(self):
        with self.assertRaises(LookupError):
            games.measure_now()
        self.play(1000, 10)
        with self.assertRaises(RuntimeError):  # PresentMon "isn't installed" in this test
            games.measure_now()

    def test_library_never_waits_for_the_disk(self):
        with mock.patch.object(games.hardware, "installed_games_cached", lambda: None):  # not read yet
            lib = games.library()
        self.assertEqual((lib["reading"], lib["games"], lib["move_first"]), (True, [], []))
        t = self.play(1000, 90)
        self.alive = False
        games.update(self.snapshot(game=False), now=t + 2.0)
        played = {**STEAM_GAME, "played": 1_760_000_000}
        with mock.patch.object(games.hardware, "installed_games_cached", lambda: [played]):
            lib = games.library()
        self.assertFalse(lib["reading"])
        [game] = lib["games"]
        self.assertEqual((game["name"], game["drive"], game["letter"], game["sessions"]), ("Test Game", "hard disk", "D:", 1))
        self.assertEqual([g["name"] for g in lib["move_first"]], ["Test Game"])  # played, on the hard disk
        self.assertEqual(lib["best_ssd"], {"letter": "C:", "kind": "NVMe SSD", "free_gb": 600})

    def test_the_game_load_includes_its_helper_processes(self):
        me = os.getpid()
        rows = [{"pid": me, "ppid": 1, "name": "game.exe", "cpu": 1.0, "mem_mb": 170.0, "io_bps": 0.0},
                {"pid": 9001, "ppid": me, "name": "game.exe", "cpu": 300.0, "mem_mb": 2000.0, "io_bps": 1e6},  # GPU process
                {"pid": 9002, "ppid": me, "name": "editor.exe", "cpu": 50.0, "mem_mb": 900.0, "io_bps": 0.0},  # own window
                {"pid": 9003, "ppid": 1, "name": "other.exe", "cpu": 80.0, "mem_mb": 500.0, "io_bps": 0.0}]
        with mock.patch.object(games.monitor, "latest_processes", lambda: (rows, me)), \
                mock.patch("app.system.processes.app_window_pids", lambda: {me, 9002}):
            load = games._game_load(games._Session(me, "game.exe", None, "Game", 0, 0, 0), self.snapshot())
        self.assertEqual(load, {"cpu": 301.0, "mem_mb": 2170.0, "io_bps": 1e6})

    def test_a_helper_started_later_is_the_games_too(self):
        tree = games._GameProcesses({os.getppid()})
        self.assertIn(os.getpid(), tree)  # started by a process of the game
        self.assertNotIn(4, tree)  # Windows' System process isn't
        self.assertNotIn("x", tree)

    def test_captures_merge_frames_over_time(self):
        a = {"frames": 100, "seconds": 2.0, "fps": 50.0, "low_1_fps": 40.0, "stutter_share": 0.1,
             "shares": {"gpu": 1.0, "cpu": 0.0, "cap": 0.0}, "cap": None, "gpu_busy": 0.95, "latency_ms": 20.0,
             "present_mode": "Hardware: Independent Flip"}
        b = {**a, "frames": 300, "seconds": 3.0, "fps": 100.0, "low_1_fps": 80.0, "stutter_share": 0.0,
             "shares": {"gpu": 0.0, "cpu": 1.0, "cap": 0.0}}
        merged = games._merge_captures([a, b])
        # 400 frames in 5 s, not the faster capture counted three times (87.5); the 1% low by time
        self.assertEqual((merged["fps"], merged["low_1_fps"], merged["worst_low_1_fps"]), (80.0, 64.0, 40.0))
        self.assertEqual(merged["stutter_share"], 0.025)  # 10 of 400 frames (by time it would say 4%)
        self.assertEqual((merged["limiter"], merged["shares"]), ("cpu", {"gpu": 0.25, "cpu": 0.75, "cap": 0.0}))

    def test_capture_mode_is_only_counted_if_unchanged(self):
        with mock.patch.object(games.focus, "mode_in_effect", return_value="maximum"):
            games._capture_focus_mode = "maximum"
            self.assertEqual(games._capture_mode(), "maximum")
            games.focus_mode_changed()  # switched while it measured (even if switched back)
            self.assertIsNone(games._capture_mode())
            games._capture_mode_mixed, games._capture_manual = False, True
            self.assertIsNone(games._capture_mode())  # one you started goes on with the game in the background
            games._capture_manual = False
        with mock.patch.object(games.focus, "mode_in_effect", return_value="off"):
            self.assertIsNone(games._capture_mode())  # another mode now than when it started

    def test_switching_focus_mode_never_waits_for_the_game_loop(self):
        held = threading.Event()
        threading.Thread(target=lambda: (games._lock.acquire(), held.wait(5), games._lock.release()), daemon=True).start()
        time.sleep(0.05)  # the game loop holds its lock (a session ending waits up to 3 s for PresentMon)
        started = time.perf_counter()
        games.focus_mode_changed()
        held.set()
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertTrue(games._capture_mode_mixed)


def summary(**extra):
    base = {"game": "Test Game", "store": "Steam", "size_gb": 40.0, "drive": "NVMe SSD", "frames": None,
            "frames_error": None, "cpu": {"busiest_core_avg": 90}, "memory": {"max_percent": 60},
            "gpu": {"name": "NVIDIA GeForce RTX 2060 SUPER", "temp_max": 70, "slowdown_c": 93, "power_limit_w": 175.0,
                    "vram_max_mb": 5000, "memory_total_mb": 8192, "reasons": {}}}
    base.update(extra)
    return base


def fr(limiter, **extra):
    shares = {"gpu": 0.0, "cpu": 0.0, "cap": 0.0, limiter: 1.0}
    return {"fps": 80.0, "low_1_fps": 60.0, "stutter_share": 0.0, "limiter": limiter, "shares": shares,
            "cap": None, "present_mode": "Hardware: Independent Flip", **extra}


FACTS = {"cores": 4, "threads": 8, "refresh_hz": 165.0, "ram_gb": 16,
         "best_ssd": {"letter": "C:", "kind": "NVMe SSD", "free_gb": 600}}


class AdviceTests(unittest.TestCase):
    def ids(self, s, facts=FACTS):
        return [a["id"] for a in games.advise(s, facts)]

    def test_a_hot_gpu_comes_first(self):
        hot = summary(frames=fr("gpu"), gpu={**summary()["gpu"], "reasons": {"thermal": 0.3, "power": 0.5}, "temp_max": 88})
        self.assertEqual(self.ids(hot)[:2], ["gpu_hot", "gpu_bound"])

    def test_gpu_at_its_power_limit(self):
        s = summary(frames=fr("gpu"), gpu={**summary()["gpu"], "reasons": {"power": 0.8}})
        text = next(a["text"] for a in games.advise(s, FACTS) if a["id"] == "gpu_bound")
        self.assertIn("175 W", text)
        self.assertIn("automatic tuning", text)

    def test_cpu_limited_on_a_four_core(self):
        text = next(a["text"] for a in games.advise(summary(frames=fr("cpu")), FACTS) if a["id"] == "cpu_bound")
        self.assertIn("faster processor", text)
        self.assertIn("raise resolution or graphics quality", text)
        text8 = next(a["text"] for a in games.advise(summary(frames=fr("cpu")), {**FACTS, "cores": 8}) if a["id"] == "cpu_bound")
        self.assertNotIn("faster processor", text8)

    def test_a_low_cap(self):
        s = summary(frames=fr("cap", cap={"kind": "cap", "fps": 60}))
        text = next(a["text"] for a in games.advise(s, FACTS) if a["id"] == "capped")
        self.assertIn("165 Hz", text)

    def test_hard_disk_stutter_and_full_memory(self):
        s = summary(drive="hard disk", frames=fr("gpu", stutter_share=0.05, low_1_fps=30.0), memory={"max_percent": 93})
        ids = self.ids(s)
        for expected in ("move_to_ssd", "stutter", "ram_full"):
            self.assertIn(expected, ids)
        stutter = next(a["text"] for a in games.advise(s, FACTS) if a["id"] == "stutter")
        self.assertIn("hard disk", stutter)

    def test_near_the_memory_limit_names_the_apps_to_close(self):
        # Real session: committed 23.2-24.3 of 25.4 GB with RAM at 91-95%, reads back from disk up to 36 MB/s
        apps = [{"name": "Code.exe", "title": "Visual Studio Code", "mb": 2232},
                {"name": "brave.exe", "title": "Brave", "mb": 1515}, {"name": "Discord.exe", "title": "Discord", "mb": 950}]
        memory = {"max_percent": 95, "commit_max_percent": 96, "commit_max_mb": 24924, "commit_limit_mb": 26020,
                  "hard_faults_max_mbps": 36.0, "apps": apps}
        advice = {a["id"]: a for a in games.advise(summary(memory=memory, frames=fr("cpu", stutter_share=0.05)), FACTS)}
        self.assertNotIn("ram_full", advice)  # the limit's tip says what it would, and more
        text = advice["commit_full"]["text"]
        self.assertIn("Up to 24.3 of 25.4 GB was promised to apps", text)
        self.assertIn("Visual Studio Code (2.2 GB), Brave (1.5 GB) and Discord (950 MB)", text)
        self.assertIn("full memory (Windows read up to 36 MB/s back from disk", advice["stutter"]["text"])
        ram_only = games.advise(summary(memory={"max_percent": 93, "apps": apps[:1]}), FACTS)
        text = next(a["text"] for a in ram_only if a["id"] == "ram_full")
        self.assertIn("The apps holding the most: Visual Studio Code (2.2 GB). Close the ones you don't need", text)
        self.assertNotIn("commit_full", [a["id"] for a in ram_only])

    def test_not_enough_room_on_the_ssd(self):
        s = summary(drive="hard disk", size_gb=700.0)
        text = next(a["text"] for a in games.advise(s, FACTS) if a["id"] == "move_to_ssd")
        self.assertIn("to an SSD", text)

    def test_unmeasured_says_why(self):
        s = summary(frames_error="measuring frame rates needs OptiMind to run as administrator")
        self.assertEqual(games.advise(s, FACTS)[0]["text"],
                         "Measuring frame rates needs OptiMind to run as administrator.")

    def test_an_unknown_recorder_by_its_name(self):
        s = summary(recording=[{"app": "capture.exe", "label": None, "encode_avg": 12, "share": 0.5}])
        advice = next(a for a in games.advise(s, FACTS) if a["id"] == "recording")
        self.assertEqual(advice["title"], "capture.exe recorded the game")
        self.assertIn("12% busy for 50% of the time", advice["text"])

    def test_full_video_memory(self):
        s = summary(frames=fr("gpu"), gpu={**summary()["gpu"], "vram_max_mb": 8000})
        self.assertIn("vram_full", self.ids(s))

    def test_full_video_memory_whatever_limited_the_game(self):
        full = {**summary()["gpu"], "vram_max_mb": 7977}  # a real session: 7977 of 8192 MB, frames not measured
        self.assertIn("vram_full", self.ids(summary(gpu=full, frames_error="no frames")))
        self.assertIn("vram_full", self.ids(summary(frames=fr("cpu"), gpu=full)))

    def test_discord_clips_is_a_known_recorder(self):
        s = summary(recording=[{"app": "discord_clips.exe", "label": "Discord Clips", "encode_avg": 6, "share": 1.0}])
        advice = next(a for a in games.advise(s, FACTS) if a["id"] == "recording")
        self.assertEqual(advice["title"], "Discord Clips recorded the game")
        self.assertIn("Clips", advice["text"])


class PageFileCheckTests(unittest.TestCase):
    MANAGED = {"system_managed": True,
               "files": [{"path": "C:\\pagefile.sys", "size_mb": 9216, "used_mb": 390, "peak_mb": 668}]}

    def check(self, paging):
        with mock.patch.object(hardware, "page_file", return_value=paging), \
                mock.patch.object(hardware, "_steam_games", lambda: []), mock.patch.object(hardware, "_epic_games", lambda: []):
            return next(c for c in hardware._storage_checks(DRIVES) if c["id"] == "pagefile")

    def test_where_it_is_and_who_sets_its_size(self):
        good = self.check(self.MANAGED)
        self.assertEqual(good["status"], "good")
        self.assertEqual(good["summary"], "The page file is on C: (NVMe SSD). Windows manages its size. It's 9.0 GB; "
                                          "at most 668 MB of it was used since the PC started.")
        fixed = self.check({**self.MANAGED, "system_managed": False})
        self.assertEqual(fixed["status"], "tip")  # it can't grow: an app that runs out of memory closes
        self.assertIn("Automatically manage paging file size", fixed["action"])
        on_hdd = self.check({**self.MANAGED, "files": [{**self.MANAGED["files"][0], "path": "D:\\pagefile.sys"}]})
        self.assertEqual((on_hdd["status"], on_hdd["summary"]), ("tip", "The page file is on a hard disk (D:)."))


class BiggestAppsTests(unittest.TestCase):
    def test_programs_by_their_own_memory(self):
        rows = [{"pid": 10, "name": "Code.exe", "private_mb": 900.0}, {"pid": 11, "name": "Code.exe", "private_mb": 1300.0},
                {"pid": 12, "name": "brave.exe", "private_mb": 1500.0}, {"pid": 13, "name": "MsMpEng.exe", "private_mb": 3000.0},
                {"pid": 14, "name": "SearchHost.exe", "private_mb": 4000.0},
                {"pid": 15, "name": "msedgewebview2.exe", "private_mb": 2500.0},
                {"pid": 16, "name": "game.exe", "private_mb": 6000.0}, {"pid": 17, "name": "tiny.exe", "private_mb": 10.0},
                {"pid": 18, "name": "unknown.exe", "private_mb": None}]
        paths = {10: r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\Code.exe",
                 12: r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                 14: r"C:\Windows\SystemApps\SearchHost.exe", 15: r"C:\Program Files (x86)\Microsoft\EdgeWebView\msedgewebview2.exe",
                 16: r"D:\Games\game.exe", 17: r"C:\Apps\tiny.exe"}
        titles = {"Code.exe": "Visual Studio Code", "brave.exe": "Brave"}
        with mock.patch.object(processes.monitor, "latest_processes", lambda: (rows, None)), \
                mock.patch.object(processes, "_exe_path", paths.get), \
                mock.patch.object(processes, "_WINDIR", "c:\\windows\\"), \
                mock.patch.object(processes, "_title", lambda name, path: titles.get(name, name)):
            self.assertEqual(processes.biggest_apps(leave_out={16}), [  # Windows' own, Defender, WebView2 and the game left out
                {"name": "Code.exe", "title": "Visual Studio Code", "mb": 2200, "processes": 2},
                {"name": "brave.exe", "title": "Brave", "mb": 1500, "processes": 1},
                {"name": "tiny.exe", "title": "tiny.exe", "mb": 10, "processes": 1}])
            self.assertEqual([a["name"] for a in processes.biggest_apps(5, leave_out={16}, min_mb=100)], ["Code.exe", "brave.exe"])
            own = [{"pid": 30, "name": processes.OWN_NAME, "private_mb": 9000.0}]  # OptiMind's window, from the installed app
            paths[30] = r"C:\Program Files\OptiMind AI\OptiMind.exe"
            with mock.patch.object(processes.monitor, "latest_processes", lambda: (own + rows, None)):
                self.assertNotIn(processes.OWN_NAME, [a["name"].lower() for a in processes.biggest_apps(5)])


EVENT = ('<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
         '<Provider Name="{provider}" Guid="{{C26C4F3C-3F66-4E99-8F8A-39405CFED220}}"/><EventID{q}>{id}</EventID>'
         '<TimeCreated SystemTime="{time}"/></System>{data}</Event>')


def event(provider, event_id, when, data="", qualifiers=""):
    return EVENT.format(provider=provider, id=event_id, time=when, data=data, q=qualifiers)


class StabilityTests(unittest.TestCase):
    def test_reading_events(self):
        whea = stability.parse_event(event("Microsoft-Windows-WHEA-Logger", 19, "2026-10-17T15:09:36.4890840Z"))
        self.assertEqual((whea["category"], whea["id"]), ("hardware", 19))
        self.assertEqual(whea["time"], datetime(2026, 10, 17, 15, 9, 36, 489084, tzinfo=timezone.utc))
        crash = stability.parse_event(event("Application Error", 1000, "2026-10-10T01:02:03.0000000Z",
                                            "<EventData><Data>game.exe</Data><Data>1.0</Data></EventData>", ' Qualifiers="0"'))
        self.assertEqual((crash["category"], crash["app"]), ("app_crash", "game.exe"))
        self.assertEqual(stability.parse_event(event("Display", 4101, "2026-10-10T01:02:03Z"))["category"], "gpu_reset")
        self.assertIsNone(stability.parse_event(event("Display", 9999, "2026-10-10T01:02:03Z")))

    def test_counts(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        events = [{"category": "hardware", "id": 19, "time": now - timedelta(days=2), "app": None},
                  {"category": "hardware", "id": 17, "time": now - timedelta(days=20), "app": None},
                  {"category": "app_crash", "id": 1000, "time": now - timedelta(days=1), "app": "game.exe"}]
        c = stability.summarize(events, 30, now)["categories"]
        self.assertEqual((c["hardware"]["count"], c["hardware"]["last_7_days"], c["hardware"]["ids"]), (2, 1, {19: 1, 17: 1}))
        self.assertEqual(c["app_crash"]["apps"], {"game.exe": 1})
        self.assertEqual(c["bluescreen"]["count"], 0)


class HardwareHelperTests(unittest.TestCase):
    def test_power_setting_in_any_language(self):
        # powercfg's labels are translated; the last two hex numbers are the AC and DC values
        portuguese = ("GUID da Configuração de Energia: 6738e2c4-e8a5-4a42-b16a-e040e769756e  (Desligar disco após)\n"
                      "  Configuração mínima possível: 0x00000000\n  Configuração máxima possível: 0xffffffff\n"
                      "  Incremento de configurações possíveis: 0x00000001\n  Unidades de configurações possíveis: Segundos\n"
                      "Índice de Configuração de Energia CA Atual: 0x000004b0\n"
                      "Índice de Configuração de Energia CC Atual: 0x00000258\n")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, portuguese, "")):
            self.assertEqual(hardware.power_setting("SCHEME_CURRENT", "SUB_DISK", "DISKIDLE"), (1200, 600))
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "no such plan")):
            self.assertIsNone(hardware.power_setting("nope", "SUB_DISK", "DISKIDLE"))

    def test_drive_health(self):
        disks = [{"id": "0", "name": "Fast SSD", "health": 0, "ssd": True}, {"id": "1", "name": "Old HDD", "health": 0, "ssd": False}]
        good = hardware.drive_health(disks, {"0": {"Temperature": 40, "Wear": 3, "PowerOnHours": 1200}})
        self.assertEqual(good["status"], "good")
        self.assertIn("Fast SSD: Healthy, 40 °C, 3% worn", good["summary"])
        worn = hardware.drive_health(disks, {"0": {"Temperature": 40, "Wear": 93}})
        self.assertEqual(worn["status"], "warn")
        errors = hardware.drive_health(disks, {"1": {"Temperature": 38, "ReadErrorsUncorrected": 4}})
        self.assertEqual(errors["status"], "warn")
        self.assertIn("back up", errors["detail"])
        hot = hardware.drive_health(disks, {"1": {"Temperature": 58}})
        self.assertEqual(hot["status"], "tip")
        failing = hardware.drive_health([{**disks[1], "health": 2}], {})
        self.assertEqual(failing["status"], "warn")
        self.assertIn("administrator", hardware.drive_health(disks, {})["detail"])


class DirectXFlagTests(unittest.TestCase):
    """Windowed-games optimization and variable refresh rate share one settings string;
    each tweak owns only its own KEY, so undoing one never undoes the other."""

    def setUp(self):
        self.registry = {}
        key = ("HKCU", tweaks._DIRECTX, tweaks._DX_SETTINGS)
        read = lambda hive, path, name: (True, 1, self.registry[key]) if key in self.registry else (False, None, None)  # noqa: E731
        write = lambda hive, path, name, kind, value: self.registry.__setitem__(key, value)  # noqa: E731
        delete = lambda hive, path, name: self.registry.pop(key, None)  # noqa: E731
        for name, fake in (("_read", read), ("_write", write), ("_delete", delete)):
            patcher = mock.patch.object(tweaks, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key = key
        self.windowed, self.vrr = tweaks._BY_ID["windowed_games"], tweaks._BY_ID["vrr"]

    def apply(self, t):
        backup = tweaks._capture(t)
        self.assertEqual(tweaks._set_targets(t), ([], 1))
        return backup

    def test_strings(self):
        self.assertEqual(tweaks.flags_of("A=1;B=0;"), [("A", "1"), ("B", "0")])
        self.assertEqual(tweaks.with_flag("A=1;B=0;", "B", "1"), "A=1;B=1;")
        self.assertEqual(tweaks.with_flag("A=1;", "C", "1"), "A=1;C=1;")
        self.assertEqual(tweaks.with_flag("A=1;C=1;", "A", None), "C=1;")
        self.assertEqual(tweaks.with_flag(None, "A", "1"), "A=1;")

    def test_undo_keeps_the_other_tweak(self):
        self.registry[self.key] = "AutoHDREnable=1;"  # set by Windows Settings
        windowed_backup = self.apply(self.windowed)
        vrr_backup = self.apply(self.vrr)
        self.assertEqual(self.registry[self.key], "AutoHDREnable=1;SwapEffectUpgradeEnable=1;VRROptimizeEnable=1;")
        self.assertEqual((tweaks._state(self.windowed), tweaks._state(self.vrr)), ("applied", "applied"))
        self.assertEqual(tweaks._restore(self.windowed, windowed_backup), [])  # undo the first one applied
        self.assertEqual(self.registry[self.key], "AutoHDREnable=1;VRROptimizeEnable=1;")
        self.assertEqual(tweaks._state(self.vrr), "applied")
        self.assertEqual(tweaks._restore(self.vrr, vrr_backup), [])
        self.assertEqual(self.registry[self.key], "AutoHDREnable=1;")

    def test_a_flag_that_was_on_stays_on(self):
        self.registry[self.key] = "VRROptimizeEnable=1;"
        backup = self.apply(self.vrr)
        tweaks._restore(self.vrr, backup)
        self.assertEqual(self.registry[self.key], "VRROptimizeEnable=1;")

    def test_no_backup_and_an_empty_value_is_deleted(self):
        self.apply(self.vrr)  # the value didn't exist
        self.assertEqual(tweaks._restore(self.vrr, None), [])
        self.assertNotIn(self.key, self.registry)


class DiskSpinTweakTests(unittest.TestCase):
    def test_damaged_backup_is_refused(self):
        with mock.patch("subprocess.run") as run:
            for bad in ({"not-a-guid": 0}, {"381b4222-f694-41f0-9685-ff5bb260df2e": 10 ** 9},
                        {"381b4222-f694-41f0-9685-ff5bb260df2e": "0"}):
                with self.subTest(bad), self.assertRaises(ValueError):
                    tweaks._set_disk_idle(bad)
            run.assert_not_called()

    def test_reads_every_plan(self):
        values = {"381b4222-f694-41f0-9685-ff5bb260df2e": (1200, 600), "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": (0, 1200)}
        with mock.patch.object(tweaks, "_disk_idle_cache", None), \
                mock.patch.object(tweaks.hardware, "power_setting", lambda plan, sub, name: values.get(plan)), \
                mock.patch.object(optimizer, "active_power_plan_guid", lambda: "381b4222-f694-41f0-9685-ff5bb260df2e"):
            self.assertEqual(tweaks._disk_idle(), {"381b4222-f694-41f0-9685-ff5bb260df2e": 1200,
                                                   "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": 0})
            self.assertFalse(tweaks._disks_spinning())


def _start_gpu_process():
    """A throwaway interpreter that creates a Direct3D 11 device and waits (so Windows gives
    it a GPU scheduling priority), or None when this PC can't."""
    code = ("import ctypes, time\nd = ctypes.WinDLL('d3d11'); dev, ctx = ctypes.c_void_p(), ctypes.c_void_p()\n"
            "lvl = ctypes.c_int()\nhr = d.D3D11CreateDevice(None, 1, None, 0, None, 0, 7, ctypes.byref(dev), "
            "ctypes.byref(lvl), ctypes.byref(ctx))\nprint('ok' if hr == 0 else 'no', flush=True)\ntime.sleep(60)\n")
    proc = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", code], stdout=subprocess.PIPE, text=True)
    ready = proc.stdout.readline().strip() == "ok"
    proc.stdout.close()
    if not ready:
        proc.kill()
        proc.wait()
        return None
    return proc


@unittest.skipUnless(ON_WINDOWS, "Windows only")
class GpuPriorityTests(unittest.TestCase):
    def test_never_above_above_normal(self):
        for value in (4, 5, -1):
            with self.subTest(value), self.assertRaises(ValueError):
                optimizer._set_gpu_priority_checked(os.getpid(), value)

    def test_lowered_then_put_back(self):
        proc = _start_gpu_process()
        if proc is None:
            self.skipTest("no Direct3D 11 device on this PC")
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        target = psutil.Process(proc.pid)
        result = optimizer._lower_gpu_priority([target])
        self.assertIn("Below normal GPU priority for 1 instance (checked)", result)
        self.assertEqual(optimizer.held_back().get(target.name().lower()), ["gpu"])
        self.assertIn("GPU priority put back on 1 instance (checked)", optimizer.restore_app(target.name()))
        handle = optimizer._kernel32().OpenProcess(0x1200, False, proc.pid)
        try:
            self.assertEqual(optimizer._get_gpu_priority(handle), 2)  # Normal again
        finally:
            optimizer._kernel32().CloseHandle(handle)

    def test_a_process_without_the_gpu_is_left_alone(self):
        proc = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.5)
        self.assertIn("not using the GPU", optimizer._lower_gpu_priority([psutil.Process(proc.pid)]))


def gsnap(procs, game=True, audible=()):
    return SystemSnapshot(cpu_percent=50, ram_percent=50, ram_available_mb=4000, power_plan="High performance",
                          top_processes=list(procs), foreground_game=game, agent_is_admin=True,
                          audible_apps=list(audible))


GAME = ProcessSample(pid=10, name="game.exe", cpu_percent=200, memory_mb=4000, gpu_percent=95, is_foreground=True)
WALLPAPER = ProcessSample(pid=11, name="wallpaper64.exe", cpu_percent=2, memory_mb=300, gpu_percent=20)
OBS = ProcessSample(pid=12, name="obs64.exe", cpu_percent=5, memory_mb=300, gpu_percent=30)


class GpuPriorityEngineTests(unittest.TestCase):
    def gpu_actions(self, engine, snapshot, now, focus=True):
        """The engine's GPU-priority decisions for one cycle at `now`, as (action, target)."""
        policy = OptimizationPolicy(enable_focus_mode=focus, enable_power_plan_switch=False)
        with mock.patch("app.agent.decision_engine.time.time", return_value=now):
            return [(a.action, a.target) for a in engine.decide(snapshot, policy)
                    if a.action in (ActionType.LOWER_GPU_PRIORITY, ActionType.RESTORE_GPU_PRIORITY)]

    def test_background_gpu_work_while_playing(self):
        engine = DecisionEngine()
        self.assertEqual(self.gpu_actions(engine, gsnap([GAME, WALLPAPER, OBS]), 1000), [])  # grace period
        self.assertEqual(self.gpu_actions(engine, gsnap([GAME, WALLPAPER, OBS]), 1011),
                         [(ActionType.LOWER_GPU_PRIORITY, "wallpaper64.exe")])  # OBS records the game: left alone
        self.assertEqual(self.gpu_actions(engine, gsnap([GAME, WALLPAPER]), 1013), [])  # once
        self.assertEqual(self.gpu_actions(engine, gsnap([WALLPAPER], game=False), 1020), [])  # a quick Alt+Tab
        self.assertEqual(self.gpu_actions(engine, gsnap([WALLPAPER], game=False), 1050),
                         [(ActionType.RESTORE_GPU_PRIORITY, "wallpaper64.exe")])

    def test_apps_playing_sound_and_focus_off_are_left_alone(self):
        for label, snapshot, focus in (("playing sound", gsnap([GAME, WALLPAPER], audible=["wallpaper64.exe"]), True),
                                       ("Focus mode off", gsnap([GAME, WALLPAPER]), False)):
            engine = DecisionEngine()
            with self.subTest(label):
                for t in (1000, 1011, 1022):
                    self.assertEqual(self.gpu_actions(engine, snapshot, t, focus=focus), [])

    def test_safety_refuses_recording_apps(self):
        with self.assertRaises(SafetyViolation):
            validate_action(OptimizationAction(action=ActionType.LOWER_GPU_PRIORITY, target="OBS64.exe", reason="test"))
        with self.assertRaises(SafetyViolation):
            validate_action(OptimizationAction(action=ActionType.LOWER_GPU_PRIORITY, target="dwm.exe", reason="test"))


if __name__ == "__main__":
    unittest.main()
