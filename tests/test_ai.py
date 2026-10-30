"""The AI layer: free providers with fallback, the answer's validation and the local brain.
Every HTTP request is faked: nothing goes over the network."""
import asyncio
import json
import unittest
from datetime import datetime
from unittest import mock

import httpx

from app.agent import policy_store
from app.ai import chat, local_brain, policy_brain
from app.config import settings
from app.models import OptimizationPolicy, SystemSnapshot
from tests import api_client

GOOD_POLICY = {
    "cpu_high_threshold": 80, "ram_high_threshold": 85, "background_grace_period_seconds": 45,
    "enable_priority_management": True, "enable_ram_reclaim": True, "enable_power_plan_switch": True,
    "high_load_power_plan": "High performance", "idle_power_plan": "Balanced",
    "process_denylist_extra": ["obs64.exe"], "notes": "Tuned for test.",
}


def answer(content):
    return 200, {"choices": [{"message": {"content": content}}]}


def fake_post(responses, sent=None):
    """A stand-in for httpx.post: {url fragment: (status, json body)}; anything else can't connect."""
    def post(url, headers=None, json=None, timeout=None):
        if sent is not None:
            sent.append((url, json))
        for fragment, (status, body) in responses.items():
            if fragment in url:
                return httpx.Response(status, json=body, request=httpx.Request("POST", url))
        raise httpx.ConnectError("no route", request=httpx.Request("POST", url))
    return mock.patch("httpx.post", post)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.saved = (settings.optimind_ai_providers, settings.gemini_api_key, settings.groq_api_key)
        settings.optimind_ai_providers = "gemini,groq,local"
        settings.gemini_api_key, settings.groq_api_key = "g-test", "q-test"

    def tearDown(self):
        settings.optimind_ai_providers, settings.gemini_api_key, settings.groq_api_key = self.saved

    def test_no_keys_means_the_local_brain(self):
        settings.gemini_api_key = settings.groq_api_key = ""
        policy, provider, failures = policy_brain.review_policy(OptimizationPolicy())
        self.assertEqual((provider, failures, policy.updated_by), ("local", [], "local"))
        reply, provider = chat.ask("what did you do?")
        self.assertEqual(provider, "local")
        self.assertTrue(reply)

    def test_a_used_up_quota_falls_back_to_the_next_provider(self):
        sent = []
        with fake_post({"googleapis": (429, [{"error": {"message": "Resource has been exhausted (e.g. check quota)."}}]),
                        "groq.com": answer(json.dumps(GOOD_POLICY))}, sent):
            policy, provider, failures = policy_brain.review_policy(OptimizationPolicy())
        self.assertEqual(provider, "groq")
        self.assertIn("429", failures[0])
        self.assertEqual((policy.cpu_high_threshold, policy.updated_by), (80, "ai"))
        body = sent[0][1]
        self.assertEqual(body["response_format"]["type"], "json_schema")  # structured output, strict schema
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["model"], settings.optimind_gemini_model)

    def test_fields_the_ai_may_not_change_are_ignored(self):
        evil = dict(GOOD_POLICY, enable_startup_management=True)
        with fake_post({"googleapis": answer("```json\n" + json.dumps(evil) + "\n```")}):
            policy, provider, _ = policy_brain.review_policy(OptimizationPolicy())
        self.assertEqual(provider, "gemini")  # an answer in a ```json fence is still read
        self.assertIs(policy.enable_startup_management, False)

    def test_unsafe_or_invalid_answers_are_rejected(self):
        bad = dict(GOOD_POLICY, cpu_high_threshold=5)
        with fake_post({"googleapis": answer(json.dumps(bad)), "groq.com": answer("not json at all")}):
            _, provider, failures = policy_brain.review_policy(OptimizationPolicy())
        self.assertEqual(provider, "local")
        self.assertEqual(len(failures), 2)

    def test_chat(self):
        with fake_post({}):
            self.assertEqual(chat.ask("hello")[1], "local")  # no internet
        settings.gemini_api_key = ""
        sent = []
        with fake_post({"groq.com": answer("I lowered Chrome's priority twice.")}, sent):
            reply, provider = chat.ask("what did you do?")
        self.assertEqual(provider, "groq")
        self.assertTrue(reply.startswith("I lowered"))
        self.assertNotIn("response_format", sent[0][1])  # free text, not JSON

    def test_chat_uses_the_agents_own_snapshot(self):
        # Measuring again from the API's thread would reset the rates the agent's next cycle reads
        settings.gemini_api_key = settings.groq_api_key = ""
        latest = SystemSnapshot(cpu_percent=42, ram_percent=61, ram_available_mb=6000)
        with mock.patch.object(chat, "take_snapshot", side_effect=AssertionError("measured again")):
            reply, provider = chat.ask("how is my PC?", latest)
        self.assertEqual(provider, "local")
        self.assertIn("CPU 42%", reply)

    def test_the_chat_knows_the_pc_and_its_memory(self):
        # Real chat: the AI called this 16 GB PC "a 12-GB system" and 2.6:1 compression "the maximum"
        latest = SystemSnapshot(cpu_percent=80, ram_percent=93, ram_available_mb=1500, commit_used_mb=23860,
                                commit_limit_mb=26020, hard_faults_mb_s=36.0)
        paging = {"system_managed": True, "files": [{"path": "C:\\pagefile.sys", "size_mb": 9216, "used_mb": 390,
                                                      "peak_mb": 668}]}
        names = {"computer_name": "SOME-PC", "cpu_name": "AMD Ryzen 5 3400G", "gpu_names": ["NVIDIA GeForce RTX 2060 SUPER"]}
        with mock.patch.object(chat.hardware, "page_file", return_value=paging), \
                mock.patch.object(chat.sysinfo, "static_info", return_value=names), \
                mock.patch.object(chat.psutil, "virtual_memory", return_value=mock.Mock(total=17_045_651_456)), \
                mock.patch.object(chat.psutil, "cpu_count", lambda logical=True: 8 if logical else 4):
            context = chat._build_grounding_context(latest, OptimizationPolicy(), [])
        for fact in ("processor AMD Ryzen 5 3400G (4 cores / 8 threads)", "graphics NVIDIA GeForce RTX 2060 SUPER",
                     "RAM 15.9 GB usable", "Committed (promised to apps) 23.3 of 25.4 GB",
                     "Page file: C:\\pagefile.sys, 9.0 GB, its size managed by Windows; 390 MB in use, at most 668 MB",
                     "Read back from disk for memory right now: 36.0 MB/s"):
            self.assertIn(fact, context)
        self.assertNotIn("SOME-PC", context)  # the PC's name never goes to a provider
        self.assertIn("no fixed maximum", chat.CHAT_SYSTEM_PROMPT)

    def test_api_endpoints(self):
        from app.main import app

        client = api_client(app)  # loops not started
        settings.gemini_api_key = ""
        with fake_post({"groq.com": answer("All quiet.")}):
            r = client.post("/api/chat", json={"message": "status?"})
        self.assertEqual((r.status_code, r.json()["provider"]), (200, "groq"))
        with fake_post({}):
            r = client.post("/api/policy/refresh-now")
        self.assertEqual((r.status_code, r.json()["provider"]), (200, "local"))
        self.assertEqual(client.get("/api/status").json()["ai_last_provider"], "local")

    def test_the_ai_loop_falls_back_and_records_why(self):
        from app.service import orchestrator
        from app.service.state import app_state

        calls = 0
        real_sleep = asyncio.sleep

        async def one_round(_seconds):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError
            await real_sleep(0)

        settings.groq_api_key = ""
        with fake_post({"googleapis": (429, {"error": {"message": "quota exhausted"}})}), \
                mock.patch.object(orchestrator.asyncio, "sleep", one_round), \
                mock.patch.object(orchestrator.policy_store, "save_policy", lambda p: None):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(orchestrator.ai_loop())
        self.assertEqual(app_state.ai_last_provider, "local")
        self.assertIn("quota exhausted", app_state.ai_loop_last_error or "")


class ProtectedAppsTests(unittest.TestCase):
    """The AI's protected apps: dated, gone after 14 days, and one you took off stays off.
    Real policy: 26 apps, VS Code, Brave and Discord among them, and the list only ever grew."""

    def setUp(self):
        from app.service.state import app_state

        saved = app_state.current_policy
        self.addCleanup(setattr, app_state, "current_policy", saved)
        patcher = mock.patch.object(policy_store, "save_policy", lambda p: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_app_is_dated_and_leaves_after_14_days(self):
        now = datetime(2026, 10, 23, 12, 0)
        p = policy_store.settle_protected(OptimizationPolicy(process_denylist_extra=["Code.exe", "brave.exe", "code.exe"],
                                                             protected_since={"brave.exe": "2026-10-01T09:00:00"}), now)
        self.assertEqual(p.process_denylist_extra, ["Code.exe"])  # brave.exe was added 22 days ago; no twice
        self.assertEqual(p.protected_since, {"code.exe": "2026-10-23T12:00:00"})
        self.assertEqual(policy_store.settle_protected(p, datetime(2026, 11, 6, 11, 0)).process_denylist_extra, ["Code.exe"])
        self.assertEqual(policy_store.settle_protected(p, datetime(2026, 11, 6, 13, 0)).process_denylist_extra, [])

    def test_an_app_you_took_off_stays_off(self):
        from app.service import orchestrator
        from app.service.state import app_state

        app_state.current_policy = policy_store.settle_protected(
            OptimizationPolicy(process_denylist_extra=["Code.exe", "Discord.exe"]))
        policy = orchestrator.unprotect("code.exe")
        self.assertEqual((policy.process_denylist_extra, policy.unprotected_by_you), (["Discord.exe"], ["code.exe"]))
        with self.assertRaises(LookupError):
            orchestrator.unprotect("notepad.exe")
        # A review that was answered from the old policy lists it again: it stays off
        reviewed = policy.model_copy(update={"process_denylist_extra": ["Discord.exe", "Code.exe"], "unprotected_by_you": []})
        self.assertEqual(orchestrator.keep_user_choices(reviewed).process_denylist_extra, ["Discord.exe"])
        # ...and the built-in rules don't propose it, however often it came back to the front
        history = [{"cpu": 20.0, "ram": 60.0}] * 40
        actions = [{"action": "restore_app", "target": "Code.exe"}] * 5
        proposed = local_brain.propose_policy(policy, history, actions)
        self.assertNotIn("code.exe", [name.lower() for name in proposed.process_denylist_extra])
        self.assertNotIn("code.exe", proposed.notes.lower())

    def test_the_api(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.service.state import app_state

        app_state.current_policy = OptimizationPolicy(process_denylist_extra=["Code.exe"])
        client = api_client(app)
        r = client.post("/api/policy/unprotect", json={"name": "Code.exe"})
        self.assertEqual((r.status_code, r.json()["policy"]["process_denylist_extra"]), (200, []))
        self.assertEqual(client.post("/api/policy/unprotect", json={"name": "Code.exe"}).status_code, 404)
        anyone = TestClient(app, base_url="http://127.0.0.1:8765")  # without the window's secret
        self.assertEqual(anyone.post("/api/policy/unprotect", json={"name": "Discord.exe"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
