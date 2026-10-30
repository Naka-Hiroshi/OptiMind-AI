"""The decision engine's rules (pure logic: made-up snapshots, nothing on the PC is touched)."""
import functools
import time
import unittest

from app.agent.decision_engine import DecisionEngine
from app.agent.safety import MAX_ACTIONS_PER_CYCLE, within_limit
from app.models import (ActionLogEntry, ActionType, OptimizationAction, OptimizationPolicy, ProcessSample,
                        SystemSnapshot)


def snap(cpu=10, ram=50, plan="High performance", procs=(), idle=0.0, battery=None, free=None, standby=None,
         admin=None, game=False, recent=(), audible=()):
    return SystemSnapshot(cpu_percent=cpu, ram_percent=ram, ram_available_mb=4000, power_plan=plan,
                          top_processes=list(procs), user_idle_seconds=idle, on_battery=battery,
                          battery_percent=50 if battery is not None else None, free_mb=free, standby_mb=standby,
                          agent_is_admin=admin, foreground_game=game, recent_apps=list(recent), audible_apps=list(audible))


focus_snap = functools.partial(snap, cpu=20, plan="Balanced", admin=True)


def kinds(actions):
    return sorted(a.action.value for a in actions)


def plan_targets(engine, policy, readings):
    out = []
    for now, s in readings:
        a = engine._power_plan_action(s, policy, now)
        out.append(a.target if a else None)
    return out


def ram_acts(engine, s, policy, now, foreground=frozenset()):
    return engine._ram_actions(s, policy, set(), set(foreground), now)


def pressed(engine=None, started=0):
    """An engine that has seen the RAM at its line for a while (memory pressure has to last 10 s)."""
    engine = engine or DecisionEngine()
    engine._started = started if engine._started is None else engine._started
    engine._pressure_since = float("-inf")
    return engine


POLICY = OptimizationPolicy(enable_page_combining=False)  # CPU 85%, idle Balanced, High performance under load, grace 30 s
NO_COMPRESS = OptimizationPolicy(enable_idle_compression=False, enable_page_combining=False)
HOG = ProcessSample(pid=1, name="hog.exe", cpu_percent=50, memory_mb=100)
BIG = ProcessSample(pid=2, name="big.exe", cpu_percent=0, memory_mb=800)


class PowerPlanTests(unittest.TestCase):
    def test_balanced_once_after_three_calm_minutes(self):
        targets = plan_targets(DecisionEngine(), POLICY, [(t, snap()) for t in range(1000, 1300, 2)])
        self.assertEqual(targets.count("Balanced"), 1)
        self.assertEqual(targets.index("Balanced"), 90)  # at 1180: three minutes after the calm began

    def test_not_before_the_three_minutes_are_up(self):
        # A game's menu or a quick Alt+Tab (a minute or two of calm) keeps High performance
        self.assertEqual(plan_targets(DecisionEngine(), POLICY, [(1000, snap()), (1061, snap()), (1179, snap())]),
                         [None, None, None])

    def test_switches_then_stays_quiet(self):
        readings = [(1000, snap()), (1181, snap()), (1183, snap(plan="Balanced")), (1400, snap(plan="Balanced"))]
        self.assertEqual(plan_targets(DecisionEngine(), POLICY, readings), [None, "Balanced", None, None])

    def test_three_busy_samples_in_a_row_switch_up(self):
        readings = [(t, snap(cpu=95, plan="Balanced")) for t in (1000, 1002, 1004)]
        self.assertEqual(plan_targets(DecisionEngine(), POLICY, readings), [None, None, "High performance"])

    def test_a_short_spike_does_not_switch(self):
        readings = [(1000, snap(cpu=95, plan="Balanced")), (1002, snap(cpu=20, plan="Balanced")),
                    (1004, snap(cpu=95, plan="Balanced"))]
        self.assertEqual(plan_targets(DecisionEngine(), POLICY, readings), [None, None, None])

    def test_in_between_keeps_the_current_plan(self):
        targets = plan_targets(DecisionEngine(), POLICY, [(t, snap(cpu=75)) for t in range(1000, 1200, 2)])
        self.assertEqual(targets.count(None), 100)

    def test_a_plan_you_chose_by_hand_is_respected(self):
        readings = [(1000, snap()), (1181, snap()), (1183, snap(plan="Balanced")), (1500, snap(plan="High performance"))]
        self.assertEqual(plan_targets(DecisionEngine(), POLICY, readings), [None, "Balanced", None, None])

    def test_calm_reason_says_minutes(self):
        e = DecisionEngine()
        actions = [e._power_plan_action(snap(), POLICY, t) for t in (1000, 1181)]
        self.assertTrue(actions[1] and actions[1].reason.startswith("CPU calm (now 10%) for 3 min"))

    def test_busy_but_away_or_on_battery_stays_balanced(self):
        for label, extra in (("away 10 min", {"idle": 600}), ("on battery", {"battery": True})):
            with self.subTest(label):
                readings = [(t, snap(cpu=95, plan="Balanced", **extra)) for t in (1000, 1002, 1004)]
                self.assertEqual(plan_targets(DecisionEngine(), POLICY, readings), [None, None, None])

    def test_away_reason_explains_why(self):
        a = DecisionEngine()._power_plan_action(snap(cpu=95, plan="High performance", idle=900), POLICY, 1000)
        self.assertTrue(a and a.reason.startswith("No keyboard/mouse input for 15 min"))


class BackgroundAppTests(unittest.TestCase):
    def test_grace_period(self):
        for grace, expected in [(30.0, [False, False, True]), (5.0, [False, True, True])]:
            with self.subTest(grace=grace):
                e, p = DecisionEngine(), OptimizationPolicy(background_grace_period_seconds=grace)
                lowered = []
                for t in (1000, 1010, 1031):
                    acts = e._process_priority_actions(snap(procs=[HOG]), p, set(), set(), t)
                    lowered.append(any(a.action == "lower_priority" for a in acts) or bool(lowered and lowered[-1]))
                self.assertEqual(lowered, expected)

    def test_only_the_instance_doing_the_work_is_targeted(self):
        e = DecisionEngine()
        quiet_twin = ProcessSample(pid=7, name="hog.exe", cpu_percent=2, memory_mb=100)
        e._first_seen_hogging["hog.exe"] = 0
        acts = e._process_priority_actions(snap(procs=[HOG, quiet_twin]), POLICY, set(), set(), 100)
        self.assertEqual(sorted({tuple(a.pids) for a in acts}), [(1,)])

    def test_priority_and_efficiency_mode_are_both_undone_on_focus(self):
        e = DecisionEngine()
        p = OptimizationPolicy(background_grace_period_seconds=5, enable_power_plan_switch=False)
        e.decide(snap(procs=[HOG]), p)
        e._first_seen_hogging["hog.exe"] = time.time() - 10
        self.assertEqual(kinds(e.decide(snap(procs=[HOG]), p)), ["efficiency_mode_on", "lower_priority"])
        self.assertEqual(e.managed_processes(), {"hog.exe": ["priority", "efficiency"]})
        acts = e.decide(snap(procs=[HOG.model_copy(update={"is_foreground": True})]), p)
        self.assertEqual(kinds(acts), ["restore_app"])  # everything put back in one action
        self.assertEqual(e.managed_processes(), {})

    def test_efficiency_mode_off_in_the_policy(self):
        e = DecisionEngine()
        e._first_seen_hogging["hog.exe"] = time.time() - 60
        acts = e.decide(snap(procs=[HOG]), OptimizationPolicy(enable_efficiency_mode=False, enable_power_plan_switch=False))
        self.assertEqual(kinds(acts), ["lower_priority"])

    def test_sync_follows_what_is_really_held_back(self):
        e = DecisionEngine()
        e._deprioritized |= {"a.exe", "b.exe"}
        e.sync({"a.exe": ["priority"]}, now=1000)
        self.assertEqual(e.managed_processes(), {"a.exe": ["priority"]})
        self.assertEqual((e._paused("b.exe", 1500), e._paused("b.exe", 1601)), (True, False))  # 10-minute pause

    def test_an_app_with_an_instance_in_focus_is_left_alone(self):
        e = DecisionEngine()
        chrome_fg = ProcessSample(pid=10, name="chrome.exe", cpu_percent=5, memory_mb=300, is_foreground=True)
        chrome_bg = ProcessSample(pid=11, name="chrome.exe", cpu_percent=60, memory_mb=900)
        e._first_seen_hogging["chrome.exe"] = time.time() - 600
        e._started = time.time() - 3600
        acts = e.decide(snap(ram=95, procs=[chrome_fg, chrome_bg]), POLICY)
        self.assertEqual([a.action.value for a in acts if a.target == "chrome.exe"], [])

    def test_disk_hog_gets_low_io_priority(self):
        disk_hog = ProcessSample(pid=3, name="backup.exe", cpu_percent=1, memory_mb=100, io_bps=20 * 1024 * 1024)
        e, p = DecisionEngine(), OptimizationPolicy(background_grace_period_seconds=5)
        self.assertEqual(kinds(e._io_priority_actions(snap(procs=[disk_hog]), p, set(), set(), 1000)), [])  # grace
        acts = e._io_priority_actions(snap(procs=[disk_hog]), p, set(), set(), 1006)
        self.assertEqual([(a.action.value, a.pids) for a in acts], [("lower_io_priority", [3])])
        self.assertIn("20 MB/s", acts[0].reason)
        self.assertEqual(kinds(e._give_back_actions(snap(procs=[disk_hog]), {"backup.exe"}, set())), ["restore_app"])
        protected = DecisionEngine()._io_priority_actions(snap(procs=[disk_hog]), p, {"backup.exe"}, set(), 2000)
        self.assertEqual(kinds(protected), [])

    def test_decide_trims_a_big_idle_app_under_pressure_before_the_grace_period(self):
        e = DecisionEngine()
        e._started = time.time() - 3600
        self.assertEqual(kinds(pressed(e).decide(snap(cpu=40, ram=88, procs=[HOG, BIG]), POLICY)), ["trim_working_set"])

    def test_the_ais_protected_apps_keep_their_priority_not_their_idle_memory(self):
        # Real log: the AI had put the apps holding ~7 GB on its list, so nothing could be compressed while a game ran
        policy = OptimizationPolicy(process_denylist_extra=["Code.exe"], enable_power_plan_switch=False,
                                    enable_page_combining=False)
        e = DecisionEngine()
        e._started = time.time() - 3600
        idle = ProcessSample(pid=3, name="code.exe", cpu_percent=0.5, memory_mb=900)
        self.assertEqual(kinds(pressed(e).decide(snap(cpu=40, ram=90, procs=[idle]), policy)), ["trim_working_set"])
        e = DecisionEngine()
        e._first_seen_hogging["code.exe"] = 0
        busy = ProcessSample(pid=4, name="Code.exe", cpu_percent=60, memory_mb=100)
        self.assertEqual(kinds(e.decide(snap(procs=[busy]), policy)), [])  # its CPU priority stays


class MemoryTests(unittest.TestCase):
    def test_pressure_trim_threshold(self):
        for threshold, ram, expected in [(85.0, 80, 0), (85.0, 86, 1), (75.0, 80, 1)]:
            with self.subTest(threshold=threshold, ram=ram):
                e = pressed()
                policy = NO_COMPRESS.model_copy(update={"ram_high_threshold": threshold})
                self.assertEqual(len(ram_acts(e, snap(ram=ram, procs=[BIG]), policy, 1000)), expected)

    def test_pressure_has_to_last_and_ends_below_the_line(self):
        e = DecisionEngine()
        e._started = 0

        def trims(ram, now):
            e._last_trim.clear()  # (the same app's cooldown isn't what this is about)
            return len(ram_acts(e, snap(ram=ram, procs=[BIG]), NO_COMPRESS, now))

        self.assertEqual(trims(90, 1000), 0)  # a moment above the line (a game loading) isn't pressure
        self.assertEqual(trims(84, 1005), 0)
        self.assertEqual(trims(90, 1006), 0)  # above it again: the 10 s start over
        self.assertEqual(trims(90, 1016), 1)
        self.assertEqual(trims(82, 1020), 1)  # 3 points below the line: still pressure
        self.assertEqual(trims(79, 1030), 0)  # over once more than 5 points below
        self.assertEqual(trims(86, 1032), 0)  # and it has to last again

    def test_the_same_app_is_not_trimmed_again_within_5_minutes(self):
        e = pressed()
        counts = [len(ram_acts(e, snap(ram=90, procs=[BIG]), NO_COMPRESS, now)) for now in (1000, 1100, 1301)]
        self.assertEqual(counts, [1, 0, 1])

    def test_a_working_or_recently_used_app_is_not_trimmed(self):
        e = pressed()
        busy_big = BIG.model_copy(update={"cpu_percent": 30})
        self.assertEqual(ram_acts(e, snap(ram=95, procs=[busy_big]), NO_COMPRESS, 1000), [])
        e = pressed()
        e._last_foreground["big.exe"] = 950
        self.assertEqual(ram_acts(e, snap(ram=95, procs=[BIG]), NO_COMPRESS, 1000), [])  # used a minute ago
        self.assertEqual(len(ram_acts(e, snap(ram=95, procs=[BIG]), NO_COMPRESS, 1071)), 1)  # but not 2 minutes ago

    def test_idle_compression(self):
        idle_app = ProcessSample(pid=5, name="chat.exe", cpu_percent=0.2, memory_mb=400)
        policy = OptimizationPolicy(idle_compress_minutes=10, enable_page_combining=False)
        e = DecisionEngine()
        e._started = 0
        e._last_foreground["chat.exe"] = 1000
        self.assertEqual(ram_acts(e, snap(ram=70, procs=[idle_app]), policy, 1500), [])  # not before 10 minutes
        acts = ram_acts(e, snap(ram=70, procs=[idle_app]), policy, 1601)
        self.assertEqual([(a.action.value, a.pids) for a in acts], [("trim_working_set", [5])])
        self.assertIn("idle in the background for 10 min", acts[0].reason)
        self.assertEqual(ram_acts(e, snap(ram=70, procs=[idle_app]), policy, 2000), [])  # not again within 30 min
        self.assertEqual(len(ram_acts(e, snap(ram=70, procs=[idle_app]), policy, 3402)), 1)
        e = DecisionEngine()
        e._started = 0
        self.assertEqual(ram_acts(e, snap(ram=40, procs=[idle_app]), policy, 5000), [])  # plenty of free RAM
        e = DecisionEngine()
        e._started = 0
        off = policy.model_copy(update={"enable_idle_compression": False})
        self.assertEqual(ram_acts(e, snap(ram=70, procs=[idle_app]), off, 5000), [])

    def test_an_app_opened_later_is_unused_only_since_it_appeared(self):
        # It counted as unused since OptiMind started, so a big app opened in the background was compressed at once
        policy = OptimizationPolicy(idle_compress_minutes=10, enable_page_combining=False)
        e = DecisionEngine()
        e._started = 0
        ram_acts(e, snap(ram=70), policy, 1000)  # the first look: it isn't running yet
        launcher = ProcessSample(pid=9, name="launcher.exe", cpu_percent=0.1, memory_mb=400)
        self.assertEqual(ram_acts(e, snap(ram=70, procs=[launcher]), policy, 5000), [])  # just opened
        self.assertEqual(ram_acts(e, snap(ram=70, procs=[launcher]), policy, 5500), [])
        acts = ram_acts(e, snap(ram=70, procs=[launcher]), policy, 5601)
        self.assertEqual([a.target for a in acts], ["launcher.exe"])
        self.assertIn("idle in the background for 10 min", acts[0].reason)

    def test_at_most_3_apps_per_cycle_biggest_first(self):
        policy = OptimizationPolicy(idle_compress_minutes=10, enable_page_combining=False)
        many = [ProcessSample(pid=100 + i, name=f"app{i}.exe", cpu_percent=0, memory_mb=200 + i * 50) for i in range(6)]
        e = DecisionEngine()
        e._started = 0
        self.assertEqual([a.target for a in ram_acts(e, snap(ram=70, procs=many), policy, 5000)],
                         ["app5.exe", "app4.exe", "app3.exe"])
        self.assertEqual([a.target for a in ram_acts(e, snap(ram=70, procs=many), policy, 5002)],
                         ["app2.exe", "app1.exe", "app0.exe"])

    def test_standby_purge_only_when_it_is_worth_it(self):
        def purge(engine, s, now):
            return [a.action.value for a in ram_acts(engine, s, NO_COMPRESS, now) if a.action.value == "purge_standby_list"]

        e = pressed()
        self.assertEqual(purge(e, snap(ram=90, free=300, standby=3000, admin=True), 1000), ["purge_standby_list"])
        self.assertEqual(purge(e, snap(ram=90, free=300, standby=3000, admin=True), 1300), [])  # 10-minute cooldown
        for label, s in (("plenty free", snap(ram=90, free=800, standby=3000, admin=True)),
                         ("little standby", snap(ram=90, free=300, standby=1000, admin=True)),
                         ("no memory pressure", snap(ram=60, free=300, standby=3000, admin=True)),
                         # Real log: purged at 72-82% with 3-4.5 GB still available, 21 times in two days:
                         # Windows reuses that cache by itself, so it only made files load from the disk again
                         ("high, but not real pressure", snap(ram=82, free=2, standby=4500, admin=True)),
                         ("no administrator rights", snap(ram=90, free=300, standby=3000, admin=False))):
            with self.subTest(label):
                self.assertEqual(purge(pressed(), s, 1000), [])
        self.assertEqual(purge(DecisionEngine(), snap(ram=95, free=300, standby=3000, admin=True), 1000), [])  # a moment


GAME = ProcessSample(pid=50, name="r5apex.exe", cpu_percent=300, memory_mb=4000, is_foreground=True)
LIGHT_HOG = ProcessSample(pid=60, name="steamwebhelper.exe", cpu_percent=12, memory_mb=300)  # 12% of a core
DISCORD = ProcessSample(pid=70, name="Discord.exe", cpu_percent=25, memory_mb=500)
_OFF = dict(enable_power_plan_switch=False, enable_page_combining=False, enable_ram_reclaim=False)
BALANCED = OptimizationPolicy(**_OFF)
MAXIMUM = OptimizationPolicy(focus_mode="maximum", **_OFF)
NO_FOCUS = OptimizationPolicy(enable_focus_mode=False, **_OFF)


def after(engine, s, policy, seconds):
    """decide() again once the busy app has been seen for `seconds`."""
    engine.decide(s, policy)
    for name in list(engine._first_seen_hogging):
        engine._first_seen_hogging[name] = time.time() - seconds
    return engine.decide(s, policy)


class FocusModeTests(unittest.TestCase):
    def test_a_game_in_focus_makes_the_engine_act_sooner(self):
        playing = focus_snap(procs=[GAME, LIGHT_HOG], game=True)
        self.assertEqual(kinds(after(DecisionEngine(), playing, BALANCED, 11)), ["efficiency_mode_on", "lower_priority"])
        self.assertEqual(kinds(after(DecisionEngine(), playing, BALANCED, 8)), [])  # not before 10 s
        self.assertEqual(kinds(after(DecisionEngine(), focus_snap(procs=[GAME, LIGHT_HOG]), BALANCED, 40)), [])  # no game
        self.assertEqual(kinds(after(DecisionEngine(), playing, NO_FOCUS, 11)), [])  # Focus mode off
        acts = after(DecisionEngine(), playing, BALANCED, 11)
        self.assertIn("while r5apex.exe is in focus", acts[0].reason)
        self.assertEqual([a.target for a in acts if a.target == "r5apex.exe"], [])  # the game itself never

    def test_maximum_keeps_background_work_on_one_core_until_after_the_game(self):
        playing = focus_snap(procs=[GAME, LIGHT_HOG], game=True)
        e = DecisionEngine()
        self.assertEqual(kinds(after(e, playing, MAXIMUM, 11)), ["confine_cores", "efficiency_mode_on", "lower_priority"])
        self.assertNotIn("confine_cores", kinds(after(DecisionEngine(), playing, BALANCED, 11)))
        now = time.time()
        self.assertEqual(e._cores_release_actions(MAXIMUM, True, now), [])  # game still in focus
        self.assertEqual(e._cores_release_actions(MAXIMUM, False, now + 10), [])  # a quick Alt+Tab out
        released = e._cores_release_actions(MAXIMUM, False, now + 31)
        self.assertEqual([(a.action.value, a.target) for a in released], [("restore_cores", "steamwebhelper.exe")])
        self.assertEqual(e._cores_release_actions(MAXIMUM, False, now + 40), [])  # only once

    def test_leaving_maximum_gives_the_cores_back(self):
        e = DecisionEngine()
        after(e, focus_snap(procs=[GAME, LIGHT_HOG], game=True), MAXIMUM, 11)
        e.release_cores()
        self.assertEqual(e._cores_release_actions(BALANCED, True, time.time()), [])
        self.assertNotIn("cores", str(e.managed_processes()))

    def test_apps_playing_sound_are_never_held_back(self):
        in_call = focus_snap(procs=[GAME, DISCORD], game=True, audible=["Discord.exe"])
        self.assertEqual(kinds(after(DecisionEngine(), in_call, BALANCED, 60)), [])
        e = DecisionEngine()
        after(e, focus_snap(procs=[GAME, DISCORD], game=True), BALANCED, 11)
        self.assertEqual(e.managed_processes(), {"discord.exe": ["priority", "efficiency"]})  # held back while silent
        acts = e.decide(in_call, BALANCED)
        self.assertEqual([(a.action.value, a.target) for a in acts], [("restore_app", "Discord.exe")])
        self.assertIn("Playing sound", acts[0].reason)
        self.assertEqual(e.managed_processes(), {})

    def test_the_app_you_switch_to_gets_everything_back(self):
        switched = focus_snap(procs=[GAME.model_copy(update={"is_foreground": False}),
                                     DISCORD.model_copy(update={"is_foreground": True})])
        e = DecisionEngine()
        after(e, focus_snap(procs=[GAME, DISCORD], game=True), BALANCED, 11)
        self.assertEqual([(a.action.value, a.target) for a in e.decide(switched, BALANCED)],
                         [("restore_app", "Discord.exe")])
        e = DecisionEngine()
        after(e, focus_snap(procs=[GAME, DISCORD], game=True), BALANCED, 11)
        e.note_focus([("discord.exe", time.time(), True)])  # Focus mode put it back between two cycles
        self.assertEqual(e.managed_processes(), {})
        self.assertEqual(kinds(e.decide(switched, BALANCED)), [])  # not proposed again

    def test_failure_pause_rules_after_a_switch(self):
        e = DecisionEngine()
        e._deprioritized.add("discord.exe")
        e.note_focus([("discord.exe", 1000.0, False)])
        e.sync({}, now=1030)
        self.assertFalse(e._paused("discord.exe", 1031))  # used 30 s ago: no 10-minute pause
        e = DecisionEngine()
        e._deprioritized.add("other.exe")
        e.sync({}, now=1030)
        self.assertTrue(e._paused("other.exe", 1031))
        e = DecisionEngine()
        e._memory_lowered.add("chat.exe")
        e.sync({}, now=1030)
        self.assertFalse(e._paused("chat.exe", 1031))  # memory priority already low: not a failure

    def test_the_alt_tab_apps_stay_ready(self):
        browser = ProcessSample(pid=80, name="chrome.exe", cpu_percent=0.1, memory_mb=900)
        notes = ProcessSample(pid=81, name="notes.exe", cpu_percent=0.1, memory_mb=600)
        mem = OptimizationPolicy(enable_power_plan_switch=False, enable_page_combining=False, idle_compress_minutes=10)
        e = DecisionEngine()
        e._started = 0
        e._last_foreground.update({"chrome.exe": 100.0, "notes.exe": 100.0})
        acts = e._ram_actions(focus_snap(ram=70, procs=[browser, notes]), mem, set(), set(), 5000, kept_ready={"chrome.exe"})
        self.assertEqual([a.target for a in acts], ["notes.exe"])
        acts = pressed()._ram_actions(focus_snap(ram=95, procs=[browser]), mem, set(), set(), 5000,
                                      kept_ready={"chrome.exe"})
        self.assertEqual(acts, [])  # not even under pressure
        e = DecisionEngine()
        e._started = 0
        self.assertEqual(e._ram_actions(focus_snap(ram=70, procs=[browser]), mem, set(), {"chrome.exe"}, 5000), [])  # sound
        e = DecisionEngine()
        e._started = 0
        acts = e.decide(focus_snap(ram=70, procs=[browser], recent=["chrome.exe"]),
                        mem.model_copy(update={"enable_focus_mode": False}))
        self.assertEqual([a.target for a in acts if a.action == ActionType.TRIM_WORKING_SET], ["chrome.exe"])
        self.assertEqual(e.managed_processes(), {"chrome.exe": ["memory"]})  # remembered with low memory priority

    def test_a_game_in_focus_switches_to_high_performance(self):
        policy = OptimizationPolicy()
        e = DecisionEngine()
        a = e._power_plan_action(focus_snap(cpu=30, plan="Balanced"), policy, 1000, game=True, game_name="r5apex.exe")
        self.assertEqual(a and a.target, "High performance")
        self.assertTrue(a.reason.startswith("A game is in focus (r5apex.exe)"))
        calm = [e._power_plan_action(focus_snap(cpu=10, plan="High performance"), policy, t, game=True)
                for t in range(1002, 1200, 2)]
        self.assertEqual(calm.count(None), 99)  # stays while playing
        outs = [e._power_plan_action(focus_snap(cpu=10, plan="High performance"), policy, t) for t in range(1200, 1500, 2)]
        self.assertEqual([x.target for x in outs if x], ["Balanced"])  # the game left the front, the CPU calm 3 min
        self.assertIsNone(DecisionEngine()._power_plan_action(focus_snap(cpu=30, idle=600), policy, 1000, game=True))
        self.assertIsNone(DecisionEngine()._power_plan_action(focus_snap(cpu=30, battery=True), policy, 1000, game=True))
        acts = DecisionEngine().decide(focus_snap(cpu=30, plan="Balanced", procs=[GAME], game=True),
                                       OptimizationPolicy(enable_page_combining=False))
        self.assertEqual([a.target for a in acts if a.action == ActionType.SWITCH_POWER_PLAN], ["High performance"])
        acts = DecisionEngine().decide(focus_snap(cpu=30, plan="Balanced", procs=[GAME], game=True),
                                       OptimizationPolicy(enable_page_combining=False, enable_focus_mode=False))
        self.assertEqual([a for a in acts if a.action == ActionType.SWITCH_POWER_PLAN], [])


class PageCombiningTests(unittest.TestCase):
    POLICY = OptimizationPolicy(enable_power_plan_switch=False, enable_idle_compression=False)

    def combines(self, engine, s, now, policy=None):
        return [a.action.value for a in engine._ram_actions(s, policy or self.POLICY, set(), set(), now)
                if a.action == ActionType.COMBINE_MEMORY]

    def test_schedule(self):
        e = DecisionEngine()
        e._started = 1000
        self.assertEqual(self.combines(e, focus_snap(ram=70), 1200), [])  # not in the first 5 minutes
        self.assertEqual(self.combines(e, focus_snap(ram=70), 1301), ["combine_memory"])
        self.assertEqual(self.combines(e, focus_snap(ram=70), 2000), [])  # not again within 30 minutes
        self.assertEqual(self.combines(e, focus_snap(ram=70), 3102), ["combine_memory"])

    def test_when_not_to_merge(self):
        cases = (("RAM 55%: not worth it", focus_snap(ram=55), {}),
                 ("a game is in focus", focus_snap(ram=80), {"game": True}),
                 ("no administrator rights", focus_snap(ram=80, admin=False), {}))
        for label, s, extra in cases:
            with self.subTest(label):
                e = DecisionEngine()
                e._started = 0
                acts = e._ram_actions(s, self.POLICY, set(), set(), 5000, **extra)
                self.assertEqual([a for a in acts if a.action == ActionType.COMBINE_MEMORY], [])
        e = DecisionEngine()
        e._started = 0
        off = self.POLICY.model_copy(update={"enable_page_combining": False})
        self.assertEqual(self.combines(e, focus_snap(ram=80), 5000, off), [])

    def test_not_while_a_game_runs_or_the_processor_is_busy(self):
        # Real log: it ran during an Alt+Tab out of a game, 17.5 s on a 4-core processor already at 94-99%
        e = DecisionEngine()
        e._started = 0
        behind = focus_snap(ram=80).model_copy(update={"game_running": True})  # the game behind another window
        self.assertEqual(self.combines(e, behind, 5000), [])
        self.assertEqual(self.combines(e, focus_snap(ram=80, cpu=70), 5002), [])  # the game closed, the processor busy
        results = [self.combines(e, focus_snap(ram=80), t) for t in range(5004, 5034, 2)]
        self.assertEqual(results[-1], ["combine_memory"])  # once the last 30 s were calm
        self.assertEqual(sum(map(len, results)), 1)

    def test_a_refusal_pauses_it_for_hours_but_a_failed_purge_does_not(self):
        e = DecisionEngine()
        e._started = 0
        e.forget("memory pages", ActionType.COMBINE_MEMORY, now=5000)
        self.assertEqual(self.combines(e, focus_snap(ram=80), 5000 + 3600), [])
        self.assertEqual(self.combines(e, focus_snap(ram=80), 5000 + 6 * 3600 + 1), ["combine_memory"])
        e = DecisionEngine()
        e._started = 0
        e.forget("system", ActionType.PURGE_STANDBY_LIST, now=5000)
        self.assertEqual(self.combines(e, focus_snap(ram=80), 5001), ["combine_memory"])


class FailureTests(unittest.TestCase):
    """What the engine does when Windows refuses a change, or an app may not be changed."""
    POLICY = OptimizationPolicy(background_grace_period_seconds=5)

    @staticmethod
    def snapshot(*procs, ram=50):
        return SystemSnapshot(cpu_percent=30, ram_percent=ram, ram_available_mb=4000, power_plan="Balanced",
                              top_processes=list(procs))

    def test_an_app_it_may_not_change_is_left_alone(self):
        blocked = ProcessSample(pid=4608, name="ThirdPartyAV.exe", cpu_percent=60, memory_mb=900, io_bps=50 * 2**20,
                                blocked_reason="no permission: ...")
        e = DecisionEngine()
        e._first_seen_hogging["thirdpartyav.exe"] = e._first_seen_io["thirdpartyav.exe"] = 0
        acts = e.decide(self.snapshot(blocked, ram=95), self.POLICY)
        self.assertEqual([a.action.value for a in acts if a.target == "ThirdPartyAV.exe"], [])

    def test_a_failed_change_is_dropped_and_paused(self):
        hog = ProcessSample(pid=10, name="render.exe", cpu_percent=60, memory_mb=100)
        e = DecisionEngine()
        e._first_seen_hogging["render.exe"] = 0
        acts = e._process_priority_actions(self.snapshot(hog), self.POLICY, set(), set(), 100)
        self.assertEqual(kinds(acts), ["efficiency_mode_on", "lower_priority"])
        e.forget("render.exe", ActionType.EFFICIENCY_MODE_ON, now=100)
        self.assertEqual(e.managed_processes(), {"render.exe": ["priority"]})  # only that measure dropped
        acts = e._give_back_actions(self.snapshot(hog.model_copy(update={"is_foreground": True})), {"render.exe"}, set())
        self.assertEqual(kinds(acts), ["restore_app"])  # still restored when it comes to the front
        e.forget("render.exe", ActionType.LOWER_PRIORITY, now=200)
        self.assertEqual(e._process_priority_actions(self.snapshot(hog), self.POLICY, set(), set(), 300), [])
        acts = e._process_priority_actions(self.snapshot(hog), self.POLICY, set(), set(), 810)
        acts += e._process_priority_actions(self.snapshot(hog), self.POLICY, set(), set(), 816)
        self.assertIn("lower_priority", kinds(acts))  # tries again after the pause and the grace period

    def test_standby_purge_rules(self):
        cache_full = {"free_mb": 300, "standby_mb": 3000}
        e = pressed(started=None)  # (just started: no page combining yet)
        e.forget("system", ActionType.PURGE_STANDBY_LIST, now=1000)
        admin = self.snapshot(ram=95).model_copy(update={**cache_full, "agent_is_admin": True})
        self.assertEqual(e._ram_actions(admin, self.POLICY, set(), set(), 1100), [])  # paused after a failure
        not_admin = self.snapshot(ram=95).model_copy(update={**cache_full, "agent_is_admin": False})
        self.assertEqual(pressed(started=None)._ram_actions(not_admin, self.POLICY, set(), set(), 1000), [])
        self.assertEqual(kinds(pressed(started=None)._ram_actions(admin, self.POLICY, set(), set(), 1000)),
                         ["purge_standby_list"])

    def test_a_process_that_wasnt_found_isnt_picked_again(self):
        # Real log: a Steam helper was "not found" every 30 minutes for 5 hours (Windows listed it under another name)
        from app.service import orchestrator

        helper = ProcessSample(pid=4321, name="SteamServiceTmp.exe", cpu_percent=0, memory_mb=400)
        policy = OptimizationPolicy(idle_compress_minutes=5, enable_page_combining=False)
        e = DecisionEngine()
        e._started = 0
        [act] = e._ram_actions(self.snapshot(helper, ram=70), policy, set(), set(), 5000)
        e.unreachable(act, now=5000)
        e._last_trim.clear()
        self.assertEqual(e._ram_actions(self.snapshot(helper, ram=70), policy, set(), set(), 7000), [])
        e._unreachable[(4321, "steamservicetmp.exe")] = time.time() - 6 * 3600 - 1
        e.decide(self.snapshot(ram=50), policy)  # forgotten after 6 hours
        self.assertEqual(e._unreachable, {})
        entry = ActionLogEntry(action=ActionType.TRIM_WORKING_SET, target="SteamServiceTmp.exe", reason="t", applied=False,
                               result="process not found (it may have already exited)")
        orchestrator._release_failed([entry], [act])
        self.assertIn((4321, "steamservicetmp.exe"), orchestrator._engine._unreachable)
        orchestrator._engine._unreachable.clear()

    def test_the_orchestrator_tells_the_engine_about_failed_changes(self):
        from app.service import orchestrator

        orchestrator._engine._efficiency.add("busy.exe")
        orchestrator._release_failed([ActionLogEntry(action=ActionType.EFFICIENCY_MODE_ON, target="busy.exe", reason="t",
                                                     applied=False, result="error: not changed, no permission")])
        self.assertNotIn("busy.exe", orchestrator._engine.managed_processes())

    def test_an_app_that_chose_its_own_setting_is_left_alone_for_an_hour(self):
        # Real log: lower_gpu_priority on discord_clips.exe (Realtime on purpose) every 10 minutes, all evening
        from app.service import orchestrator

        orchestrator._engine._gpu_lowered.add("clips.exe")
        orchestrator._release_failed([ActionLogEntry(
            action=ActionType.LOWER_GPU_PRIORITY, target="clips.exe", reason="t", applied=True,
            result="nothing changed: already low, set high on purpose, or not using the GPU")])
        self.assertNotIn("clips.exe", orchestrator._engine.managed_processes())
        self.assertGreater(orchestrator._engine._paused_until["clips.exe"], time.time() + 3000)


def hold(name, kind=ActionType.LOWER_PRIORITY):
    return OptimizationAction(action=kind, target=name, reason="t")


class PerCycleLimitTests(unittest.TestCase):
    def test_giving_back_never_waits(self):
        changes = [hold(f"app{i}.exe") for i in range(MAX_ACTIONS_PER_CYCLE + 3)]
        give_back = [hold("game-helper.exe", ActionType.RESTORE_CORES), hold("wallpaper.exe", ActionType.RESTORE_GPU_PRIORITY)]
        now, later = within_limit(changes[:5] + give_back + changes[5:])
        self.assertEqual(len(later), 3)
        self.assertEqual(len(now), MAX_ACTIONS_PER_CYCLE + 2)
        self.assertTrue(all(a in now for a in give_back))

    def test_what_waited_is_proposed_again_next_cycle(self):
        hogs = [ProcessSample(pid=100 + i, name=f"hog{i}.exe", cpu_percent=60, memory_mb=100) for i in range(6)]
        e, policy = DecisionEngine(), OptimizationPolicy(background_grace_period_seconds=0, enable_power_plan_switch=False,
                                                         enable_ram_reclaim=False)
        first = e.decide(snap(procs=hogs), policy)  # 6 apps x (priority + efficiency) = 12
        _, later = within_limit(first)
        self.assertEqual(len(later), 2)
        e.postpone(later)
        again = e.decide(snap(procs=hogs), policy)
        self.assertEqual(sorted((a.action, a.target) for a in again), sorted((a.action, a.target) for a in later))


if __name__ == "__main__":
    unittest.main()
