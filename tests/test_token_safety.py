import asyncio
import copy
import json
import importlib
import os
from pathlib import Path
import unittest
from unittest import mock


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_A = "So11111111111111111111111111111111111111112"
TOKEN_B = "H9mU11111111111111111111111111111111111bonk"

os.environ.setdefault("INPUT_MINT", USDC_MINT)
os.environ.setdefault("OUTPUT_MINT", TOKEN_A)

import token_safety


def provider_payload(mint=TOKEN_A, score=4.5):
    return {
        "token": {"mint": mint},
        "pools": [
            {
                "poolId": "pool-a",
                "liquidity": {"usd": 125_000},
                "security": {"mintAuthority": None, "freezeAuthority": None},
            },
            {
                "poolId": "pool-b",
                "liquidity": {"usd": 25_000},
                "security": {"mintAuthority": None, "freezeAuthority": None},
            },
        ],
        "risk": {
            "score": score,
            "rugged": False,
            "jupiterVerified": True,
            "top10": 34.2,
            "dev": {"percentage": 1.8},
            "insiders": {"totalPercentage": 7.4},
            "snipers": {"totalPercentage": 2.1},
            "bundlers": {"totalPercentage": 3.6},
            "risks": [{"name": "Holder concentration", "description": "Review the top holders", "level": "warning"}],
        },
        "holders": 881,
    }


class TokenSafetyNormalizationTests(unittest.TestCase):
    def test_normalizes_one_comprehensive_response(self):
        report = token_safety.normalize_token_safety(provider_payload(), TOKEN_A)
        self.assertEqual(report["level"], "medium")
        self.assertEqual(report["score"], 4.5)
        self.assertEqual(report["authorities"], {"mint": "disabled", "freeze": "disabled"})
        self.assertEqual(report["pools"]["count"], 2)
        self.assertEqual(report["pools"]["total_liquidity_usd"], 150_000)
        self.assertEqual(report["concentration"]["insiders"], 7.4)
        self.assertEqual(report["holders"], 881)

    def test_rugged_overrides_provider_score(self):
        payload = provider_payload(score=1)
        payload["risk"]["rugged"] = True
        self.assertEqual(token_safety.normalize_token_safety(payload, TOKEN_A)["level"], "critical")

    def test_rejects_wrong_mint_and_invalid_score(self):
        with self.assertRaises(token_safety.TokenSafetyError):
            token_safety.normalize_token_safety(provider_payload(TOKEN_B), TOKEN_A)
        payload = provider_payload()
        payload["risk"]["score"] = float("nan")
        with self.assertRaises(token_safety.TokenSafetyError):
            token_safety.normalize_token_safety(payload, TOKEN_A)

    def test_missing_pool_security_is_unknown_not_safe(self):
        payload = provider_payload()
        payload["pools"][1].pop("security")
        report = token_safety.normalize_token_safety(payload, TOKEN_A)
        self.assertEqual(report["authorities"]["mint"], "unknown")
        self.assertEqual(report["authorities"]["freeze"], "unknown")

    def test_malformed_authority_and_overflowed_liquidity_stay_unknown(self):
        payload = provider_payload()
        payload["pools"][0]["security"]["mintAuthority"] = False
        payload["pools"][0]["liquidity"]["usd"] = 1e308
        payload["pools"][1]["liquidity"]["usd"] = 1e308
        report = token_safety.normalize_token_safety(payload, TOKEN_A)
        self.assertEqual(report["authorities"]["mint"], "unknown")
        self.assertIsNone(report["pools"]["total_liquidity_usd"])
        self.assertTrue(token_safety.math.isfinite(report["pools"]["largest_liquidity_usd"]))

    def test_duplicate_pools_do_not_inflate_liquidity(self):
        payload = provider_payload()
        payload["pools"].append(dict(payload["pools"][0]))
        report = token_safety.normalize_token_safety(payload, TOKEN_A)
        self.assertEqual(report["pools"]["count"], 2)
        self.assertEqual(report["pools"]["total_liquidity_usd"], 150_000)

    def test_fetch_uses_exactly_one_throttled_request(self):
        response = mock.Mock()
        response.headers = {"content-length": "100"}
        response.content = b"{}"
        response.json.return_value = provider_payload()
        with mock.patch.object(token_safety, "throttle") as throttle_mock, mock.patch.object(token_safety.requests, "get", return_value=response) as get_mock:
            report = token_safety.fetch_token_safety("secret", TOKEN_A)
        throttle_mock.assert_called_once_with()
        get_mock.assert_called_once()
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(report["token_mint"], TOKEN_A)


class TokenSafetySchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = importlib.import_module("main")

    def setUp(self):
        prefix = Path(__file__).resolve().parent.parent / f".token-safety-tests-{os.getpid()}"
        self.config_path = Path(f"{prefix}-config.json")
        self.state_path = Path(f"{prefix}-state.json")
        for path in (self.config_path, self.state_path, Path(f"{self.config_path}.lock"), Path(f"{self.state_path}.lock")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.path_patch = mock.patch.multiple(
            self.main,
            config_json_path=str(self.config_path),
            shared_json_path=str(self.state_path),
            SOLANATRACKER_API_KEY="test-key",
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        for path in (self.config_path, self.state_path, Path(f"{self.config_path}.lock"), Path(f"{self.state_path}.lock")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def write_json(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def config(self, interval=24):
        return {
            "solanatracker_features_enabled": True,
            "token_safety_enabled": True,
            "token_safety_interval_hours": interval,
            "tokens": [
                {"mint": TOKEN_A, "enabled": True, "safety_enabled": True, "runtime_nonce": "generation-a", "safety_refresh_nonce": ""},
                {"mint": TOKEN_B, "enabled": True, "safety_enabled": True, "runtime_nonce": "generation-b", "safety_refresh_nonce": ""},
            ],
        }

    def test_manual_mode_makes_no_automatic_request(self):
        self.assertIsNone(self.main.token_safety_candidates(self.config(interval=0), shared={}))

    def test_missing_persisted_safety_settings_fall_back_to_env_defaults(self):
        cfg = self.config()
        cfg.pop("token_safety_enabled")
        cfg.pop("token_safety_interval_hours")
        with mock.patch.multiple(
            self.main,
            TOKEN_SAFETY_ENABLED_DEFAULT=True,
            TOKEN_SAFETY_INTERVAL_HOURS_DEFAULT=12,
        ):
            self.assertTrue(self.main.token_safety_is_globally_enabled(cfg))
            self.assertEqual(self.main.token_safety_interval_hours(cfg), 12)

    def test_master_switch_missing_key_and_explicit_safety_off_block_checks(self):
        cfg = self.config()
        cfg["solanatracker_features_enabled"] = False
        self.assertIsNone(self.main.token_safety_candidates(cfg, shared={}))
        cfg["solanatracker_features_enabled"] = True
        cfg["token_safety_enabled"] = False
        with mock.patch.object(self.main, "TOKEN_SAFETY_ENABLED_DEFAULT", True):
            self.assertIsNone(self.main.token_safety_candidates(cfg, shared={}))
        cfg["token_safety_enabled"] = True
        with mock.patch.object(self.main, "SOLANATRACKER_API_KEY", ""):
            self.assertIsNone(self.main.token_safety_candidates(cfg, shared={}))

    def test_manual_nonce_takes_priority_and_disabled_tokens_are_skipped(self):
        cfg = self.config()
        cfg["tokens"][0]["safety_enabled"] = False
        cfg["tokens"][1]["safety_refresh_nonce"] = "manual-b"
        due = self.main.token_safety_candidates(cfg, shared={})
        self.assertEqual(due["mint"], TOKEN_B)
        self.assertEqual(due["_safety_requested_nonce"], "manual-b")

    def test_recent_attempt_staggers_scheduled_checks(self):
        cfg = self.config(interval=24)
        now = self.main.datetime.now(self.main.timezone.utc)
        shared = {"token_states": {
            TOKEN_A: {"runtime_nonce": "generation-a", "safety": {"last_attempt_at": now.isoformat()}},
            TOKEN_B: {"runtime_nonce": "generation-b", "safety": {"last_attempt_at": (now - self.main.timedelta(hours=25)).isoformat()}},
        }}
        self.assertEqual(self.main.token_safety_candidates(cfg, shared=shared, now=now)["mint"], TOKEN_B)

    def test_abandoned_scheduled_check_is_recovered_without_waiting_a_day(self):
        cfg = self.config(interval=24)
        now = self.main.datetime.now(self.main.timezone.utc)
        shared = {"token_states": {
            TOKEN_A: {
                "runtime_nonce": "generation-a",
                "safety": {
                    "status": "checking",
                    "last_attempt_at": (now - self.main.timedelta(minutes=3)).isoformat(),
                },
            },
            TOKEN_B: {
                "runtime_nonce": "generation-b",
                "safety": {"last_attempt_at": now.isoformat()},
            },
        }}
        self.assertEqual(self.main.token_safety_candidates(cfg, shared=shared, now=now)["mint"], TOKEN_A)

    def test_failed_refresh_preserves_previous_trusted_report(self):
        cfg = self.config()
        self.write_json(self.config_path, cfg)
        self.write_json(self.state_path, {"token_states": {TOKEN_A: {
            "runtime_nonce": "generation-a",
            "safety": {"status": "fresh", "report": {"score": 2, "level": "low"}, "last_success_at": "2026-08-01T00:00:00+00:00"},
        }}})
        token = {**cfg["tokens"][0], "_safety_requested_nonce": "", "_safety_interval_hours": 24}
        self.assertTrue(self.main.persist_token_safety_result(token, error="temporary provider failure"))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))["token_states"][TOKEN_A]["safety"]
        self.assertEqual(saved["status"], "stale")
        self.assertEqual(saved["report"]["score"], 2)
        self.assertEqual(saved["last_success_at"], "2026-08-01T00:00:00+00:00")

    def test_delete_readd_generation_cannot_receive_stale_result(self):
        cfg = self.config()
        old_token = {**cfg["tokens"][0], "_safety_requested_nonce": "", "_safety_interval_hours": 24}
        cfg["tokens"][0]["runtime_nonce"] = "generation-new"
        self.write_json(self.config_path, cfg)
        self.write_json(self.state_path, {"token_states": {TOKEN_A: {"runtime_nonce": "generation-new"}}})
        self.assertFalse(self.main.persist_token_safety_result(old_token, report={"score": 1, "level": "low"}))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))["token_states"][TOKEN_A]
        self.assertNotIn("safety", saved)

    def test_changed_refresh_nonce_discards_inflight_result(self):
        cfg = self.config()
        cfg["tokens"][0]["safety_refresh_nonce"] = "new-request"
        self.write_json(self.config_path, cfg)
        self.write_json(self.state_path, {"token_states": {TOKEN_A: {"runtime_nonce": "generation-a"}}})
        in_flight = {**cfg["tokens"][0], "safety_refresh_nonce": "old-request", "_safety_requested_nonce": "old-request"}
        self.assertFalse(self.main.persist_token_safety_result(in_flight, report={"score": 1, "level": "low"}))

    def test_stale_candidate_cannot_mark_a_readded_token_as_checking(self):
        cfg = self.config()
        stale_candidate = {**cfg["tokens"][0], "_safety_requested_nonce": "", "_safety_interval_hours": 24}
        cfg["tokens"][0]["runtime_nonce"] = "generation-new"
        self.write_json(self.config_path, cfg)
        initial_state = {"token_states": {TOKEN_A: {"runtime_nonce": "generation-new"}}}
        self.write_json(self.state_path, initial_state)
        self.assertFalse(self.main.write_token_safety_checking(stale_candidate))
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), initial_state)


class TokenSafetyBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        os.environ["FRONTEND_DIR"] = str(root / "frontend_app" / "dist")
        os.environ["CONFIG_PATH"] = str(root / f".token-safety-backend-{os.getpid()}-config.json")
        os.environ["SHARED_STATE_PATH"] = str(root / f".token-safety-backend-{os.getpid()}-state.json")
        cls.backend = importlib.import_module("backend_api")

    def setUp(self):
        self.original_state = copy.deepcopy(self.backend.state)
        self.original_config_path = self.backend.CONFIG_PATH
        self.original_state_path = self.backend.STATE_PATH
        root = Path(__file__).resolve().parent.parent
        self.config_path = root / f".token-safety-backend-{os.getpid()}-config.json"
        self.state_path = root / f".token-safety-backend-{os.getpid()}-state.json"
        self.backend.CONFIG_PATH = str(self.config_path)
        self.backend.STATE_PATH = str(self.state_path)
        self.api_key_patch = mock.patch.dict(os.environ, {"SOLANATRACKER_API_KEY": "test-key"})
        self.api_key_patch.start()
        token = self.backend.normalize_token_entry({
            "mint": TOKEN_A,
            "name": "Token A",
            "enabled": True,
            "safety_enabled": True,
            "runtime_nonce": "backend-generation",
        })
        self.backend.state.update({
            "tokens": [token],
            "active_token_mint": TOKEN_A,
            "solanatracker_features_enabled": True,
            "token_safety_enabled": True,
            "token_safety_interval_hours": 24,
            "community_rules_enabled": False,
        })
        self.state_path.write_text(json.dumps({"token_states": {TOKEN_A: {
            "runtime_nonce": "backend-generation",
            "safety": {
                "status": "fresh",
                "last_success_at": "2026-08-01T10:00:00+00:00",
                "report": token_safety.normalize_token_safety(provider_payload(), TOKEN_A),
            },
        }}}), encoding="utf-8")

    def tearDown(self):
        self.api_key_patch.stop()
        self.backend.state.clear()
        self.backend.state.update(self.original_state)
        self.backend.CONFIG_PATH = self.original_config_path
        self.backend.STATE_PATH = self.original_state_path
        for path in (self.config_path, self.state_path, Path(f"{self.config_path}.lock"), Path(f"{self.state_path}.lock")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def test_normal_state_contains_compact_summary_but_modal_endpoint_has_report(self):
        summary = self.backend.get_token_state_summary()[0]["safety"]
        self.assertEqual(summary["score"], 4.5)
        self.assertNotIn("report", summary)
        detail = asyncio.run(self.backend.get_token_safety(TOKEN_A))
        self.assertEqual(detail["report"]["holders"], 881)
        self.assertTrue(detail["effective_enabled"])

    def test_per_token_safety_defaults_on_and_can_be_disabled(self):
        self.assertTrue(self.backend.normalize_token_entry({"mint": TOKEN_A})["safety_enabled"])
        payload = self.backend.TokenUpdatePayload(safety_enabled=False)
        if not hasattr(payload, "model_fields_set"):
            payload.__fields_set__ = {"safety_enabled"}
        result = asyncio.run(self.backend.update_token(TOKEN_A, payload))
        self.assertFalse(result["token"]["safety_enabled"])

    def test_invalid_interval_cannot_partially_enable_safety(self):
        self.backend.state["token_safety_enabled"] = False
        payload = self.backend.RuntimeSettings(token_safety_enabled=True, token_safety_interval_hours=721)
        for field in self.backend.RuntimeSettings.__annotations__:
            if not hasattr(payload, field):
                setattr(payload, field, None)
        if not hasattr(payload, "model_fields_set"):
            payload.__fields_set__ = {"token_safety_enabled", "token_safety_interval_hours"}
        with self.assertRaises(self.backend.HTTPException):
            asyncio.run(self.backend.update_settings(payload))
        self.assertFalse(self.backend.state["token_safety_enabled"])

    def test_enabling_manual_mode_does_not_queue_an_automatic_check(self):
        self.backend.state["token_safety_enabled"] = False
        self.backend.state["token_safety_interval_hours"] = 24
        self.backend.state["tokens"][0]["safety_refresh_nonce"] = ""
        payload = self.backend.RuntimeSettings(token_safety_enabled=True, token_safety_interval_hours=0)
        for field in self.backend.RuntimeSettings.__annotations__:
            if not hasattr(payload, field):
                setattr(payload, field, None)
        if not hasattr(payload, "model_fields_set"):
            payload.__fields_set__ = {"token_safety_enabled", "token_safety_interval_hours"}
        asyncio.run(self.backend.update_settings(payload))
        self.assertTrue(self.backend.state["token_safety_enabled"])
        self.assertEqual(self.backend.state["token_safety_interval_hours"], 0)
        self.assertEqual(self.backend.state["tokens"][0].get("safety_refresh_nonce"), "")

    def test_explicit_global_disable_is_persisted_over_the_enabled_default(self):
        payload = self.backend.RuntimeSettings(token_safety_enabled=False)
        for field in self.backend.RuntimeSettings.__annotations__:
            if not hasattr(payload, field):
                setattr(payload, field, None)
        if not hasattr(payload, "model_fields_set"):
            payload.__fields_set__ = {"token_safety_enabled"}
        asyncio.run(self.backend.update_settings(payload))
        self.assertFalse(self.backend.state["token_safety_enabled"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIs(saved["token_safety_enabled"], False)
        self.backend.state["token_safety_enabled"] = True
        self.backend.load_state()
        self.assertFalse(self.backend.state["token_safety_enabled"])

    def test_manual_refresh_deduplicates_an_inflight_scheduled_check(self):
        before_nonce = self.backend.state["tokens"][0].get("safety_refresh_nonce", "")
        cached = json.loads(self.state_path.read_text(encoding="utf-8"))
        cached["token_states"][TOKEN_A]["safety"]["status"] = "checking"
        cached["token_states"][TOKEN_A]["safety"]["last_attempt_at"] = self.backend.datetime.now(self.backend.timezone.utc).isoformat()
        self.state_path.write_text(json.dumps(cached), encoding="utf-8")
        result = asyncio.run(self.backend.refresh_token_safety(TOKEN_A))
        self.assertTrue(result["already_queued"])
        self.assertEqual(self.backend.state["tokens"][0].get("safety_refresh_nonce", ""), before_nonce)

    def test_manual_refresh_recovers_an_abandoned_check(self):
        cached = json.loads(self.state_path.read_text(encoding="utf-8"))
        cached_safety = cached["token_states"][TOKEN_A]["safety"]
        cached_safety["status"] = "checking"
        cached_safety["last_attempt_at"] = "2026-07-31T00:00:00+00:00"
        self.state_path.write_text(json.dumps(cached), encoding="utf-8")
        result = asyncio.run(self.backend.refresh_token_safety(TOKEN_A))
        self.assertFalse(result["already_queued"])
        self.assertTrue(self.backend.state["tokens"][0].get("safety_refresh_nonce"))


if __name__ == "__main__":
    unittest.main()
