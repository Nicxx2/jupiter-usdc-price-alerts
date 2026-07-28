import asyncio
import copy
from datetime import datetime, timedelta, timezone
import os
import unittest

import jupiter_quote
from community_rules import (
    advance_alert_state,
    evaluate_rules,
    mark_alert_delivery,
    normalize_rules_config,
    stale_rules_state,
)
from jupiter_quote import JupiterQuoteError, normalized_price_impact, price_impact_percent
from tests.test_scheduler_rate_limit import import_backend_module, import_main_module


MINT = "So11111111111111111111111111111111111111112"


def config_for(*items, alert_enabled=False, alert_mode="once"):
    return normalize_rules_config({
        "enabled": True,
        "alert_enabled": alert_enabled,
        "alert_mode": alert_mode,
        "items": [
            {"type": rule_type, "enabled": True, "target": target}
            for rule_type, target in items
        ],
    }, strict=True)


def token_info(**overrides):
    payload = {
        "id": MINT,
        "holderCount": 1000,
        "mcap": 1_000_000,
        "liquidity": 100_000,
        "usdPrice": 1,
        "decimals": 9,
        "stats24h": {"buyVolume": 55_000, "sellVolume": 45_000},
        "updatedAt": "2026-07-23T12:00:00Z",
    }
    payload.update(overrides)
    return payload


class CommunityRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_jupiter_key = os.environ.get("JUPITER_API_KEY")
        os.environ["JUPITER_API_KEY"] = "test-key"
        cls.main = import_main_module()

    @classmethod
    def tearDownClass(cls):
        if cls.original_jupiter_key is None:
            os.environ.pop("JUPITER_API_KEY", None)
        else:
            os.environ["JUPITER_API_KEY"] = cls.original_jupiter_key

    def test_threshold_boundaries_are_inclusive(self):
        config = config_for(
            ("min_holders", 1000),
            ("min_market_cap", 1_000_000),
            ("min_liquidity", 100_000),
            ("min_volume_24h", 100_000),
            ("max_sell_pressure_24h", 45),
            ("min_volume_liquidity_ratio", 1),
            ("max_price_impact", 0.5),
        )
        result = evaluate_rules(config, token_info(), {"value": 0.5})

        self.assertEqual(result["status"], "pass")
        self.assertTrue(all(item["status"] == "pass" for item in result["items"]))

    def test_each_rule_fails_on_the_wrong_side_of_its_threshold(self):
        cases = [
            ("min_holders", 1000, token_info(holderCount=999), None),
            ("min_market_cap", 1_000_000, token_info(mcap=999_999), None),
            ("min_liquidity", 100_000, token_info(liquidity=99_999), None),
            ("min_volume_24h", 100_000, token_info(stats24h={"buyVolume": 50_000, "sellVolume": 49_999}), None),
            ("max_sell_pressure_24h", 45, token_info(stats24h={"buyVolume": 54_000, "sellVolume": 46_000}), None),
            ("min_volume_liquidity_ratio", 1, token_info(liquidity=100_001), None),
            ("max_price_impact", 0.5, token_info(), {"value": 0.5001}),
        ]

        for rule_type, target, info, impact in cases:
            with self.subTest(rule_type=rule_type):
                result = evaluate_rules(config_for((rule_type, target)), info, impact)
                self.assertEqual(result["status"], "fail")
                self.assertEqual(result["items"][0]["status"], "fail")

    def test_market_cap_never_falls_back_to_fdv(self):
        info = token_info(fdv=9_000_000)
        info.pop("mcap")
        result = evaluate_rules(config_for(("min_market_cap", 1_000_000)), info, None)

        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["items"][0]["current"])
        self.assertIn("market cap", result["items"][0]["reason"].lower())

    def test_zero_denominators_are_unknown(self):
        info = token_info(liquidity=0, stats24h={"buyVolume": 0, "sellVolume": 0})
        result = evaluate_rules(
            config_for(("max_sell_pressure_24h", 45), ("min_volume_liquidity_ratio", 1)),
            info,
            None,
        )

        self.assertEqual(result["status"], "unknown")
        self.assertTrue(all(item["status"] == "unknown" for item in result["items"]))

    def test_boolean_market_values_and_targets_are_rejected(self):
        result = evaluate_rules(
            config_for(("min_holders", 1), ("max_price_impact", 1)),
            token_info(holderCount=True),
            {"value": True},
        )

        self.assertEqual(result["status"], "unknown")
        self.assertTrue(all(item["status"] == "unknown" for item in result["items"]))
        with self.assertRaises(ValueError):
            normalize_rules_config({"enabled": True, "items": [{"type": "min_holders", "target": True}]}, strict=True)
        self.assertFalse(normalize_rules_config({"enabled": float("nan")})["enabled"])

    def test_numeric_helpers_reject_malformed_types_and_overflow(self):
        huge = 10 ** 10000
        self.assertEqual(self.main.coerce_float(True, 7), 7)
        self.assertEqual(self.main.coerce_float(huge, 7), 7)
        self.assertIsNone(self.main._optional_decimals(True))
        self.assertIsNone(self.main._optional_decimals(6.5))

        backend = import_backend_module()
        self.assertEqual(backend.coerce_float(True, 7), 7)
        self.assertEqual(backend.coerce_float(huge, 7), 7)
        self.assertIsNone(backend._optional_decimals(True))
        self.assertIsNone(backend._optional_decimals(6.5))

    def test_impossible_negative_and_overflow_values_are_unknown(self):
        negative = evaluate_rules(
            config_for(
                ("min_holders", 1), ("min_market_cap", 1), ("min_liquidity", 1),
                ("min_volume_24h", 1), ("max_sell_pressure_24h", 45),
                ("min_volume_liquidity_ratio", 1), ("max_price_impact", 1),
            ),
            token_info(holderCount=-1, mcap=-1, liquidity=-1, stats24h={"buyVolume": -1, "sellVolume": -1}),
            {"value": -0.1},
        )

        self.assertEqual(negative["status"], "unknown")
        self.assertTrue(all(item["status"] == "unknown" for item in negative["items"]))

        overflow_total = evaluate_rules(
            config_for(("min_volume_24h", 1), ("max_sell_pressure_24h", 45)),
            token_info(stats24h={"buyVolume": 1e308, "sellVolume": 1e308}),
            None,
        )
        self.assertTrue(all(item["status"] == "unknown" for item in overflow_total["items"]))

        overflow_ratio = evaluate_rules(
            config_for(("min_volume_liquidity_ratio", 1)),
            token_info(liquidity=1e-308, stats24h={"buyVolume": 5e307, "sellVolume": 5e307}),
            None,
        )
        self.assertEqual(overflow_ratio["status"], "unknown")

        huge_integer = evaluate_rules(
            config_for(("min_holders", 1)),
            token_info(holderCount=10 ** 10000),
            None,
        )
        self.assertEqual(huge_integer["status"], "unknown")

    def test_price_impact_units_are_percentage_points(self):
        self.assertEqual(price_impact_percent({"priceImpact": "0.5"}), 0.5)
        self.assertEqual(price_impact_percent({"priceImpact": "-0.5"}), 0.5)
        self.assertEqual(price_impact_percent({
            "priceImpact": "0.5",
            "inUsdValue": "100",
            "outUsdValue": "90",
        }), 0.5)
        self.assertIsNone(price_impact_percent({"priceImpact": True, "inUsdValue": 100, "outUsdValue": 99}))
        self.assertIsNone(price_impact_percent({"priceImpact": 10 ** 10000}))
        self.assertIsNone(price_impact_percent({"priceImpact": "invalid", "priceImpactPct": "0.005"}))
        self.assertEqual(price_impact_percent({"priceImpact": None, "priceImpactPct": "0.005"}), 0.5)
        self.assertIsNone(price_impact_percent({"priceImpactPct": True, "inUsdValue": 100, "outUsdValue": 99}))
        self.assertIsNone(price_impact_percent({"inUsdValue": True, "outUsdValue": 0}))
        self.assertEqual(price_impact_percent({"priceImpactPct": "0.005"}), 0.5)
        self.assertEqual(price_impact_percent({"priceImpactPct": "-0.005"}), 0.5)
        self.assertIsNone(price_impact_percent({"inUsdValue": "100", "outUsdValue": "99"}))
        self.assertIsNone(price_impact_percent({"inUsdValue": "100", "outUsdValue": "101"}))
        self.assertEqual(normalized_price_impact({"priceImpact": "-0.5"}), -0.005)
        self.assertEqual(normalized_price_impact({"priceImpactPct": "-0.005"}), -0.005)
        self.assertEqual(normalized_price_impact({"priceImpact": "0.5", "priceImpactPct": "0.9"}), 0.005)
        self.assertIsNone(normalized_price_impact({"inUsdValue": "100", "outUsdValue": "99"}))
        self.assertIsNone(price_impact_percent({}))
        self.assertIsNone(price_impact_percent("malformed"))

    def test_malformed_price_impact_payload_stays_unknown(self):
        result = evaluate_rules(
            config_for(("max_price_impact", 0.5)),
            token_info(),
            "malformed",
        )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["items"][0]["status"], "unknown")
        self.assertNotIn("price_impact", result)

    def test_strict_config_rejects_invalid_rules(self):
        with self.assertRaises(ValueError):
            normalize_rules_config({
                "enabled": True,
                "items": [{"type": "max_sell_pressure_24h", "enabled": True, "target": 101}],
            }, strict=True)
        with self.assertRaises(ValueError):
            normalize_rules_config({"enabled": True, "items": []}, strict=True)

    def test_alert_requires_two_passes_and_rearm_requires_two_failures(self):
        config = config_for(("min_holders", 1000), alert_enabled=True, alert_mode="rearm")
        passed = evaluate_rules(config, token_info(), None)
        runtime, send = advance_alert_state({}, passed, config)
        self.assertFalse(send)
        runtime, send = advance_alert_state(runtime, passed, config)
        self.assertTrue(send)
        runtime = mark_alert_delivery(runtime, success=True, sent_at="2026-07-23T12:00:00Z")
        self.assertFalse(runtime["alert_state"]["armed"])

        failed = evaluate_rules(config, token_info(holderCount=999), None)
        runtime, send = advance_alert_state(runtime, failed, config)
        self.assertFalse(send)
        self.assertFalse(runtime["alert_state"]["armed"])
        runtime, send = advance_alert_state(runtime, failed, config)
        self.assertFalse(send)
        self.assertTrue(runtime["alert_state"]["armed"])

        runtime, send = advance_alert_state(runtime, passed, config)
        self.assertFalse(send)
        runtime, send = advance_alert_state(runtime, passed, config)
        self.assertTrue(send)

    def test_stale_gap_breaks_confirmation_streak(self):
        config = config_for(("min_holders", 1000), alert_enabled=True)
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(minutes=10)).isoformat()
        first = evaluate_rules(config, token_info(), None, evaluated_at=old_time)
        runtime, send = advance_alert_state(
            {}, first, config, now=old_time, max_gap_seconds=360,
        )
        self.assertFalse(send)

        current_time = now.isoformat()
        current = evaluate_rules(config, token_info(), None, evaluated_at=current_time)
        runtime, send = advance_alert_state(
            runtime, current, config, now=current_time, max_gap_seconds=360,
        )
        self.assertFalse(send)
        self.assertEqual(runtime["pass_streak"], 1)

        next_time = (now + timedelta(minutes=2)).isoformat()
        next_pass = evaluate_rules(config, token_info(), None, evaluated_at=next_time)
        runtime, send = advance_alert_state(
            runtime, next_pass, config, now=next_time, max_gap_seconds=360,
        )
        self.assertTrue(send)

    def test_delivery_failure_retries_but_successful_once_alert_stays_done(self):
        config = config_for(("min_holders", 1000), alert_enabled=True)
        passed = evaluate_rules(config, token_info(), None)
        runtime, _ = advance_alert_state({}, passed, config)
        runtime, send = advance_alert_state(runtime, passed, config)
        self.assertTrue(send)

        runtime = mark_alert_delivery(runtime, success=False, error="temporary ntfy failure")
        self.assertTrue(runtime["alert_state"]["armed"])
        runtime, send = advance_alert_state(runtime, passed, config)
        self.assertTrue(send)

        runtime = mark_alert_delivery(runtime, success=True)
        runtime, send = advance_alert_state(runtime, passed, config)
        self.assertFalse(send)
        self.assertTrue(runtime["alert_state"]["fired"])
        self.assertFalse(runtime["alert_state"]["armed"])

    def test_corrupt_persisted_streak_is_reset_safely(self):
        config = config_for(("min_holders", 1000), alert_enabled=True)
        passed = evaluate_rules(config, token_info(), None)
        for bad_pass, bad_fail in (("not-a-number", object()), (True, 1.5), (-1, float("inf"))):
            previous = {**passed, "pass_streak": bad_pass, "fail_streak": bad_fail}
            runtime, send = advance_alert_state(previous, passed, config)
            self.assertFalse(send)
            self.assertEqual(runtime["pass_streak"], 1)

    def test_unknown_never_fires_or_rearms(self):
        config = config_for(("min_holders", 1000), alert_enabled=True, alert_mode="rearm")
        passed = evaluate_rules(config, token_info(), None)
        runtime, _ = advance_alert_state({}, passed, config)
        runtime, _ = advance_alert_state(runtime, passed, config)
        runtime = mark_alert_delivery(runtime, success=True)

        unknown = evaluate_rules(config, None, None, fetch_error="upstream unavailable")
        for _ in range(3):
            runtime, send = advance_alert_state(runtime, unknown, config)
            self.assertFalse(send)
        self.assertFalse(runtime["alert_state"]["armed"])

    def test_stale_state_becomes_unknown(self):
        config = config_for(("min_holders", 1000))
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        runtime = evaluate_rules(config, token_info(), None, evaluated_at=old)
        stale = stale_rules_state(runtime, config, stale_after_seconds=60)

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["status"], "unknown")
        self.assertEqual(stale["items"][0]["status"], "unknown")

    def test_fresh_waiting_transition_is_not_reported_as_unavailable(self):
        config = config_for(("min_holders", 1000))
        now = datetime.now(timezone.utc)
        waiting = {
            "status": "waiting",
            "items": [],
            "evaluated_at": None,
            "status_updated_at": now.isoformat(),
        }
        fresh = stale_rules_state(waiting, config, stale_after_seconds=60, now=now)

        self.assertEqual(fresh["status"], "waiting")
        self.assertFalse(fresh["stale"])
        waiting["status_updated_at"] = (now - timedelta(minutes=10)).isoformat()
        stale_wait = stale_rules_state(waiting, config, stale_after_seconds=60, now=now)
        self.assertEqual(stale_wait["status"], "unknown")

    def test_rule_tokens_exclude_disabled_and_have_no_active_fallback(self):
        enabled_rules = config_for(("min_holders", 1000))
        cfg = {
            "tokens": [
                {"mint": MINT, "enabled": False, "rules_config": enabled_rules},
                {"mint": "OtherMint", "enabled": True, "rules_config": {"enabled": False}},
            ]
        }
        result = self.main.configured_rule_tokens(cfg)

        self.assertEqual(result, [])
        self.assertEqual(self.main.configured_rule_tokens({"tokens": []}), [])
        self.assertEqual(self.main.configured_rule_tokens({
            "community_rules_enabled": False,
            "tokens": [{"mint": MINT, "rules_config": enabled_rules}],
        }), [])
        original_key = os.environ.get("JUPITER_API_KEY")
        try:
            os.environ.pop("JUPITER_API_KEY", None)
            self.assertEqual(self.main.configured_rule_tokens(cfg), [])
        finally:
            if original_key is None:
                os.environ.pop("JUPITER_API_KEY", None)
            else:
                os.environ["JUPITER_API_KEY"] = original_key

    def test_global_disabled_state_cannot_remain_ready(self):
        config = config_for(("min_holders", 1000), alert_enabled=True)
        passed = evaluate_rules(config, token_info(), None)
        runtime, _ = advance_alert_state({}, passed, config)
        runtime, _ = advance_alert_state(runtime, passed, config)
        self.assertTrue(runtime["confirmed_ready"])

        disabled = stale_rules_state(runtime, config, stale_after_seconds=60, global_enabled=False)

        self.assertEqual(disabled["status"], "global_disabled")
        self.assertFalse(disabled["confirmed_ready"])
        self.assertEqual(disabled["items"], [])

    def test_no_enabled_rules_make_no_upstream_call(self):
        original_fetch = self.main.get_token_information
        original_refresh = self.main.LAST_COMMUNITY_RULES_REFRESH
        calls = []
        try:
            self.main.get_token_information = lambda mints: calls.append(mints)
            self.main.LAST_COMMUNITY_RULES_REFRESH = 0
            refreshed = self.main.refresh_community_rules({"tokens": [{"mint": MINT, "rules_config": {"enabled": False}}]}, force=True)
            self.assertFalse(refreshed)
            self.assertEqual(calls, [])
        finally:
            self.main.get_token_information = original_fetch
            self.main.LAST_COMMUNITY_RULES_REFRESH = original_refresh

    def test_rule_alert_topic_uses_its_config_snapshot(self):
        original_topic = self.main.NTFY_TOPIC
        original_post = self.main.requests.post
        captured = []

        class SuccessfulResponse:
            def raise_for_status(self):
                return None

        try:
            self.main.NTFY_TOPIC = "stale-global-topic"
            self.main.requests.post = lambda url, **kwargs: captured.append((url, kwargs)) or SuccessfulResponse()

            delivered = self.main.send_alert(
                "Action Rules Passed",
                "All checks passed",
                token_config={"mint": MINT, "name": "SOL", "ntfy_topic": ""},
                inherited_topic="current-config-topic",
            )

            self.assertTrue(delivered)
            self.assertEqual(captured[0][0], "https://ntfy.sh/current-config-topic")
            self.assertIn(b"Topic source: inherited", captured[0][1]["data"])

            captured.clear()
            delivered = self.main.send_alert(
                "Action Rules Passed",
                "All checks passed",
                token_config={"mint": MINT, "name": "SOL", "ntfy_topic": ""},
                inherited_topic="",
            )
            self.assertFalse(delivered)
            self.assertEqual(captured, [])

        finally:
            self.main.NTFY_TOPIC = original_topic
            self.main.requests.post = original_post
    def test_metadata_requests_are_batched_at_one_hundred_mints(self):
        calls = []
        originals = {
            "get_token_information": self.main.get_token_information,
            "read_json_file": self.main.read_json_file,
            "write_rule_states": self.main.write_rule_states,
            "last_refresh": self.main.LAST_COMMUNITY_RULES_REFRESH,
            "last_signature": self.main.LAST_COMMUNITY_RULES_CONFIG_SIGNATURE,
        }
        tokens = [
            {"mint": f"Mint{index:03d}", "rules_config": config_for(("min_holders", 1))}
            for index in range(201)
        ]
        try:
            self.main.get_token_information = lambda mints: calls.append(list(mints)) or {}
            self.main.read_json_file = lambda _path: {"token_states": {}}
            self.main.write_rule_states = lambda _updates: None
            self.main.LAST_COMMUNITY_RULES_REFRESH = 0
            self.main.LAST_COMMUNITY_RULES_CONFIG_SIGNATURE = ""

            refreshed = self.main.refresh_community_rules({"tokens": tokens}, force=True)

            self.assertTrue(refreshed)
            self.assertEqual([len(batch) for batch in calls], [100, 100, 1])
        finally:
            self.main.get_token_information = originals["get_token_information"]
            self.main.read_json_file = originals["read_json_file"]
            self.main.write_rule_states = originals["write_rule_states"]
            self.main.LAST_COMMUNITY_RULES_REFRESH = originals["last_refresh"]
            self.main.LAST_COMMUNITY_RULES_CONFIG_SIGNATURE = originals["last_signature"]

    def test_missing_jupiter_key_fails_before_request(self):
        original_key = os.environ.get("JUPITER_API_KEY")
        original_get = jupiter_quote.requests.get
        original_throttle = jupiter_quote.community_rules_throttle
        calls = []
        try:
            os.environ.pop("JUPITER_API_KEY", None)
            jupiter_quote.requests.get = lambda *args, **kwargs: calls.append("request")
            jupiter_quote.community_rules_throttle = lambda: calls.append("throttle")

            with self.assertRaisesRegex(JupiterQuoteError, "key"):
                jupiter_quote.get_token_information([MINT])
            with self.assertRaisesRegex(JupiterQuoteError, "key"):
                jupiter_quote.get_quote("InputMint", "OutputMint", 1000, community_rules_request=True)


            self.assertEqual(calls, [])
        finally:
            if original_key is None:
                os.environ.pop("JUPITER_API_KEY", None)
            else:
                os.environ["JUPITER_API_KEY"] = original_key
            jupiter_quote.requests.get = original_get
            jupiter_quote.community_rules_throttle = original_throttle

    def test_nontransient_jupiter_error_is_not_retried(self):
        original_key = os.environ.get("JUPITER_API_KEY")
        original_get = jupiter_quote.requests.get
        original_throttle = jupiter_quote.community_rules_throttle
        original_legacy_throttle = jupiter_quote.throttle
        calls = []

        class UnauthorizedResponse:
            status_code = 401
            headers = {}

            def raise_for_status(self):
                raise jupiter_quote.requests.HTTPError("401")

        try:
            os.environ["JUPITER_API_KEY"] = "test-key"
            def fake_get(*args, **kwargs):
                calls.append(("request", kwargs.get("headers", {}).get("x-api-key")))
                return UnauthorizedResponse()
            jupiter_quote.requests.get = fake_get
            jupiter_quote.community_rules_throttle = lambda: calls.append("rules")
            jupiter_quote.throttle = lambda: calls.append("legacy")

            with self.assertRaises(JupiterQuoteError):
                jupiter_quote.get_token_information([MINT])

            self.assertEqual(calls, ["rules", ("request", "test-key")])
        finally:
            if original_key is None:
                os.environ.pop("JUPITER_API_KEY", None)
            else:
                os.environ["JUPITER_API_KEY"] = original_key
            jupiter_quote.requests.get = original_get
            jupiter_quote.community_rules_throttle = original_throttle
            jupiter_quote.throttle = original_legacy_throttle

    def test_nontransient_error_does_not_block_a_later_scheduled_quote(self):
        original_get = jupiter_quote.requests.get
        original_throttle = jupiter_quote.throttle
        calls = []

        class Response:
            headers = {}

            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self.payload = payload or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise jupiter_quote.requests.HTTPError(str(self.status_code))

            def json(self):
                return self.payload

        try:
            responses = [Response(400), Response(200, {"outAmount": "123"})]
            jupiter_quote.requests.get = lambda *args, **kwargs: calls.append(kwargs) or responses.pop(0)
            jupiter_quote.throttle = lambda: None

            with self.assertRaises(JupiterQuoteError):
                jupiter_quote.get_quote("InputMint", "OutputMint", 1000)
            later_quote = jupiter_quote.get_quote("InputMint", "OutputMint", 1000)

            self.assertEqual(later_quote["outAmount"], "123")
            self.assertEqual(len(calls), 2)
        finally:
            jupiter_quote.requests.get = original_get
            jupiter_quote.throttle = original_throttle

    def test_malformed_successful_quote_response_fails_cleanly(self):
        original_get = jupiter_quote.requests.get
        original_throttle = jupiter_quote.throttle
        calls = []

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        try:
            responses = [Response([]), Response({"outAmount": None}), Response({"outAmount": True}), Response({"outAmount": 1.5})]
            jupiter_quote.requests.get = lambda *args, **kwargs: calls.append(kwargs) or responses.pop(0)
            jupiter_quote.throttle = lambda: None

            with self.assertRaisesRegex(JupiterQuoteError, "invalid quote response"):
                jupiter_quote.get_quote("InputMint", "OutputMint", 1000)
            with self.assertRaisesRegex(JupiterQuoteError, "invalid output amount"):
                jupiter_quote.get_quote("InputMint", "OutputMint", 1000)
            with self.assertRaisesRegex(JupiterQuoteError, "invalid output amount"):
                jupiter_quote.get_quote("InputMint", "OutputMint", 1000)
            with self.assertRaisesRegex(JupiterQuoteError, "invalid output amount"):
                jupiter_quote.get_quote("InputMint", "OutputMint", 1000)

            self.assertEqual(len(calls), 4)
        finally:
            jupiter_quote.requests.get = original_get
            jupiter_quote.throttle = original_throttle

    def test_global_toggle_updates_every_coin_and_forces_refresh(self):
        backend = import_backend_module()
        config = config_for(("min_holders", 1000))
        captured = []
        events = []
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "enabled": backend.state.get("community_rules_enabled", True),
            "had_nonce": "community_rules_refresh_nonce" in backend.state,
            "nonce": backend.state.get("community_rules_refresh_nonce"),
            "write_config": backend.write_config,
            "update_token_cached_states": backend.update_token_cached_states,
        }
        try:
            backend.state["tokens"] = [{"mint": MINT, "rules_config": config}]
            backend.state["community_rules_enabled"] = True
            backend.write_config = lambda: events.append("config")
            backend.update_token_cached_states = lambda token, **kwargs: (
                events.append("cache"),
                captured.append((token["mint"], kwargs)),
            )

            settings = backend.RuntimeSettings(community_rules_enabled=False)
            settings.__fields_set__ = {"community_rules_enabled"}
            asyncio.run(backend.update_settings(settings))

            self.assertFalse(backend.state["community_rules_enabled"])
            self.assertTrue(backend.state["community_rules_refresh_nonce"])
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0][1]["rules_changed"])
            self.assertFalse(captured[0][1]["rules_global_enabled"])
            self.assertEqual(events, ["config", "cache"])
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["community_rules_enabled"] = originals["enabled"]
            if originals["had_nonce"]:
                backend.state["community_rules_refresh_nonce"] = originals["nonce"]
            else:
                backend.state.pop("community_rules_refresh_nonce", None)
            backend.write_config = originals["write_config"]
            backend.update_token_cached_states = originals["update_token_cached_states"]

    def test_global_toggle_preserves_completed_once_alert(self):
        backend = import_backend_module()
        config = config_for(("min_holders", 1000), alert_enabled=True)
        passed = evaluate_rules(config, token_info(), None)
        runtime, _ = advance_alert_state({}, passed, config)
        runtime, _ = advance_alert_state(runtime, passed, config)
        runtime = mark_alert_delivery(runtime, success=True, sent_at="2026-07-23T12:00:00Z")
        stored = {"token_states": {MINT: {"rules_state": runtime}}}
        captured = {}

        class DummyLock:
            def __enter__(self):
                return None
            def __exit__(self, exc_type, exc, tb):
                return False

        originals = {
            "read_json_file": backend.read_json_file,
            "atomic_write_json": backend.atomic_write_json,
            "json_file_lock": backend.json_file_lock,
        }
        try:
            backend.read_json_file = lambda _path: copy.deepcopy(stored)
            backend.atomic_write_json = lambda _path, data: captured.update(copy.deepcopy(data))
            backend.json_file_lock = lambda _path: DummyLock()

            backend.update_token_cached_states(
                {"mint": MINT, "rules_config": config},
                rules_changed=True,
                rules_global_enabled=False,
            )

            next_runtime = captured["token_states"][MINT]["rules_state"]
            self.assertEqual(next_runtime["status"], "global_disabled")
            self.assertTrue(next_runtime["alert_state"]["fired"])
            self.assertFalse(next_runtime["alert_state"]["armed"])
            self.assertEqual(next_runtime["alert_state"]["last_sent_at"], "2026-07-23T12:00:00Z")
            self.assertEqual(next_runtime["pass_streak"], 0)
        finally:
            backend.read_json_file = originals["read_json_file"]
            backend.atomic_write_json = originals["atomic_write_json"]
            backend.json_file_lock = originals["json_file_lock"]

    def test_unrelated_token_edit_does_not_reset_unchanged_rules(self):
        backend = import_backend_module()
        config = config_for(("min_holders", 1000), alert_enabled=True)
        token = backend.normalize_token_entry({
            "mint": MINT,
            "name": "Before",
            "rules_config": config,
        })
        captured = []
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active": backend.state.get("active_token_mint"),
            "write_config": backend.write_config,
            "update_token_cached_states": backend.update_token_cached_states,
            "get_token_state_summary": backend.get_token_state_summary,
        }
        try:
            backend.state["tokens"] = [token]
            backend.state["active_token_mint"] = None
            backend.write_config = lambda: None
            backend.update_token_cached_states = lambda _token, **kwargs: captured.append(kwargs)
            backend.get_token_state_summary = lambda: []
            payload = backend.TokenUpdatePayload(name="After", rules_config=copy.deepcopy(config))
            payload.__fields_set__ = {"name", "rules_config"}

            asyncio.run(backend.update_token(MINT, payload))

            self.assertEqual(token["name"], "After")
            self.assertEqual(len(captured), 1)
            self.assertFalse(captured[0]["rules_changed"])
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active"]
            backend.write_config = originals["write_config"]
            backend.update_token_cached_states = originals["update_token_cached_states"]
            backend.get_token_state_summary = originals["get_token_state_summary"]

    def test_global_rules_cannot_enable_without_key(self):
        backend = import_backend_module()
        original_key = os.environ.get("JUPITER_API_KEY")
        original_enabled = backend.state.get("community_rules_enabled", False)
        try:
            os.environ.pop("JUPITER_API_KEY", None)
            backend.state["community_rules_enabled"] = False
            settings = backend.RuntimeSettings(community_rules_enabled=True)
            settings.__fields_set__ = {"community_rules_enabled"}

            with self.assertRaises(backend.HTTPException) as raised:
                asyncio.run(backend.update_settings(settings))

            self.assertEqual(raised.exception.status_code, 400)
            self.assertFalse(backend.state["community_rules_enabled"])
        finally:
            backend.state["community_rules_enabled"] = original_enabled
            if original_key is None:
                os.environ.pop("JUPITER_API_KEY", None)
            else:
                os.environ["JUPITER_API_KEY"] = original_key
    def test_rejected_settings_save_does_not_partially_toggle_global_rules(self):
        backend = import_backend_module()
        originals = {
            "enabled": backend.state.get("community_rules_enabled", False),
            "output_decimals": backend.state.get("output_decimals"),
            "had_nonce": "community_rules_refresh_nonce" in backend.state,
            "nonce": backend.state.get("community_rules_refresh_nonce"),
        }
        try:
            backend.state["community_rules_enabled"] = True
            backend.state["output_decimals"] = 9
            settings = backend.RuntimeSettings(
                community_rules_enabled=False,
                output_decimals=99,
            )
            settings.__fields_set__ = {"community_rules_enabled", "output_decimals"}

            with self.assertRaises(backend.HTTPException) as raised:
                asyncio.run(backend.update_settings(settings))

            self.assertEqual(raised.exception.status_code, 400)
            self.assertTrue(backend.state["community_rules_enabled"])
            self.assertEqual(backend.state["output_decimals"], 9)
            self.assertEqual(
                backend.state.get("community_rules_refresh_nonce"),
                originals["nonce"],
            )
        finally:
            backend.state["community_rules_enabled"] = originals["enabled"]
            backend.state["output_decimals"] = originals["output_decimals"]
            if originals["had_nonce"]:
                backend.state["community_rules_refresh_nonce"] = originals["nonce"]
            else:
                backend.state.pop("community_rules_refresh_nonce", None)

    def test_stale_metadata_is_not_used_for_tracked_usdc_amount(self):
        config = config_for(("max_price_impact", 0.5))
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        captured = {}
        originals = {
            "get_token_information": self.main.get_token_information,
            "fetch_rule_price_impact": self.main.fetch_rule_price_impact,
            "read_json_file": self.main.read_json_file,
            "write_rule_states": self.main.write_rule_states,
            "last_refresh": self.main.LAST_COMMUNITY_RULES_REFRESH,
        }

        def fake_price_impact(_token, metadata, _config):
            captured["token_info"] = copy.deepcopy(metadata)
            return {"value": 0.1, "checked_at": datetime.now(timezone.utc).isoformat()}

        try:
            self.main.get_token_information = lambda _mints: {
                MINT: token_info(updatedAt=old),
            }
            self.main.fetch_rule_price_impact = fake_price_impact
            self.main.read_json_file = lambda _path: {"token_states": {}}
            self.main.write_rule_states = lambda updates: captured.update({
                "updates": copy.deepcopy(updates),
            })
            self.main.LAST_COMMUNITY_RULES_REFRESH = 0

            refreshed = self.main.refresh_community_rules({
                "tokens": [{"mint": MINT, "rules_config": config}],
            }, force=True)

            self.assertTrue(refreshed)
            self.assertEqual(captured["token_info"], {"decimals": 9})
            self.assertEqual(captured["updates"][MINT]["status"], "pass")
        finally:
            self.main.get_token_information = originals["get_token_information"]
            self.main.fetch_rule_price_impact = originals["fetch_rule_price_impact"]
            self.main.read_json_file = originals["read_json_file"]
            self.main.write_rule_states = originals["write_rule_states"]
            self.main.LAST_COMMUNITY_RULES_REFRESH = originals["last_refresh"]


    def test_price_writes_preserve_rule_runtime(self):
        existing = {"token_states": {MINT: {"rules_state": {"status": "pass", "confirmed_ready": True}}}}
        captured = {}

        class DummyLock:
            def __enter__(self):
                return None
            def __exit__(self, exc_type, exc, tb):
                return False

        originals = {
            "read_json_file": self.main.read_json_file,
            "atomic_write_json": self.main.atomic_write_json,
            "json_file_lock": self.main.json_file_lock,
            "resolve_token_decimals": self.main.resolve_token_decimals,
            "output_mint": self.main.OUTPUT_MINT,
            "active_token_config": copy.deepcopy(self.main.ACTIVE_TOKEN_CONFIG),
        }
        try:
            self.main.OUTPUT_MINT = MINT
            self.main.ACTIVE_TOKEN_CONFIG = {"mint": MINT, "name": "SOL"}
            self.main.read_json_file = lambda path: copy.deepcopy(existing)
            self.main.atomic_write_json = lambda path, data: captured.update(copy.deepcopy(data))
            self.main.json_file_lock = lambda path: DummyLock()
            self.main.resolve_token_decimals = lambda mint, configured=None: 6
            self.main.write_status_json(1, 0.99, 100, 99)
            self.assertTrue(captured["token_states"][MINT]["rules_state"]["confirmed_ready"])
        finally:
            self.main.read_json_file = originals["read_json_file"]
            self.main.atomic_write_json = originals["atomic_write_json"]
            self.main.json_file_lock = originals["json_file_lock"]
            self.main.resolve_token_decimals = originals["resolve_token_decimals"]
            self.main.OUTPUT_MINT = originals["output_mint"]
            self.main.ACTIVE_TOKEN_CONFIG = originals["active_token_config"]


if __name__ == "__main__":
    unittest.main()
