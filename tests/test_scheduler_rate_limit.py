import atexit
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
_TEST_ROOT = Path(__file__).resolve().parent.parent
_TEST_PREFIX = _TEST_ROOT / f".jupiter-alert-tests-{os.getpid()}"
_TEST_CONFIG_PATH = Path(f"{_TEST_PREFIX}-config.json")
_TEST_STATE_PATH = Path(f"{_TEST_PREFIX}-state.json")
_TEST_HISTORY_PATH = Path(f"{_TEST_PREFIX}-history.sqlite3")


def cleanup_test_paths():
    base_paths = [_TEST_CONFIG_PATH, _TEST_STATE_PATH, _TEST_HISTORY_PATH]
    sidecars = [Path(f"{path}{suffix}") for path in base_paths for suffix in (".lock", "-wal", "-shm")]
    for path in base_paths + sidecars:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


atexit.register(cleanup_test_paths)


def configure_test_paths():
    os.environ["CONFIG_PATH"] = str(_TEST_CONFIG_PATH)
    os.environ["SHARED_STATE_PATH"] = str(_TEST_STATE_PATH)
    os.environ["COMMUNITY_RULES_HISTORY_PATH"] = str(_TEST_HISTORY_PATH)


def install_dependency_stubs():
    if "pandas" not in sys.modules and importlib.util.find_spec("pandas") is None:
        pandas = types.ModuleType("pandas")
        pandas.DataFrame = object
        pandas.Series = object
        sys.modules["pandas"] = pandas

    if "pydantic" not in sys.modules:
        pydantic = types.ModuleType("pydantic")

        class BaseModel:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        pydantic.BaseModel = BaseModel
        sys.modules["pydantic"] = pydantic

    if "fastapi" not in sys.modules:
        fastapi = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code=500, detail=None):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        class FastAPI:
            def add_middleware(self, *args, **kwargs):
                return None

            def post(self, *args, **kwargs):
                return lambda fn: fn

            def get(self, *args, **kwargs):
                return lambda fn: fn

            def delete(self, *args, **kwargs):
                return lambda fn: fn

            def patch(self, *args, **kwargs):
                return lambda fn: fn

            def mount(self, *args, **kwargs):
                return None

        fastapi.FastAPI = FastAPI
        fastapi.Request = object
        fastapi.HTTPException = HTTPException
        sys.modules["fastapi"] = fastapi

        middleware = types.ModuleType("fastapi.middleware")
        cors = types.ModuleType("fastapi.middleware.cors")
        cors.CORSMiddleware = object
        sys.modules["fastapi.middleware"] = middleware
        sys.modules["fastapi.middleware.cors"] = cors

        staticfiles = types.ModuleType("fastapi.staticfiles")

        class StaticFiles:
            def __init__(self, *args, **kwargs):
                pass

        staticfiles.StaticFiles = StaticFiles
        sys.modules["fastapi.staticfiles"] = staticfiles

        responses = types.ModuleType("fastapi.responses")
        responses.FileResponse = object
        sys.modules["fastapi.responses"] = responses

    sys.modules.setdefault("uvicorn", types.ModuleType("uvicorn"))


def import_main_module():
    configure_test_paths()
    install_dependency_stubs()
    os.environ.setdefault("INPUT_MINT", USDC_MINT)
    os.environ.setdefault("OUTPUT_MINT", SOL_MINT)
    os.environ.setdefault("SOLANATRACKER_RATE_LIMIT_MODE", "safe")
    os.environ.setdefault("SOLANATRACKER_REQUESTS_PER_SECOND", "1")
    return importlib.import_module("main")


def import_backend_module():
    configure_test_paths()
    install_dependency_stubs()
    os.environ.setdefault("INPUT_MINT", USDC_MINT)
    os.environ.setdefault("OUTPUT_MINT", SOL_MINT)
    os.environ.setdefault("SOLANATRACKER_RATE_LIMIT_MODE", "safe")
    os.environ.setdefault("SOLANATRACKER_REQUESTS_PER_SECOND", "1")
    return importlib.import_module("backend_api")


class SchedulerAndRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_module()
        cls.limiter = importlib.import_module("solana_rate_limiter")

    def setUp(self):
        self.main.SCHEDULER_LAST_CHECK.clear()
        self.main.SCHEDULER_TOKEN_NONCES.clear()
        self.main.PRICE_CONFIRMATIONS.clear()
        self.main.QUOTE_CACHE.clear()
        self.main.SCHEDULER_CURSOR = 0

    def test_token_intervals_inherit_global_defaults(self):
        cfg = {"check_interval": 60, "rsi_check_interval": 5, "alert_reset_minutes": 30, "rsi_interval": "1m", "rsi_reset_enabled": False}
        custom = {"mint": "TokenB", "check_interval": 10, "rsi_check_interval": 2, "alert_reset_minutes": 7, "rsi_interval": "5m", "rsi_reset_enabled": True}
        inherited = {"mint": "TokenA"}

        self.assertEqual(self.main.token_check_interval(inherited, cfg), 60)
        self.assertEqual(self.main.token_rsi_check_interval(inherited, cfg), 5)
        self.assertEqual(self.main.token_alert_reset_minutes(inherited, cfg), 30)
        self.assertEqual(self.main.token_rsi_interval(inherited, cfg), "1m")
        self.assertFalse(self.main.token_rsi_reset_enabled(inherited, cfg))
        self.assertEqual(self.main.token_check_interval(custom, cfg), 10)
        self.assertEqual(self.main.token_rsi_check_interval(custom, cfg), 2)
        self.assertEqual(self.main.token_alert_reset_minutes(custom, cfg), 7)
        self.assertEqual(self.main.token_rsi_interval(custom, cfg), "5m")
        self.assertTrue(self.main.token_rsi_reset_enabled(custom, cfg))


    def rsi_result(self, value, timestamp="2026-07-30T12:00:00+00:00", close=10.0, volume=5.0, interval="1m"):
        return value, timestamp, {
            "timestamp": timestamp,
            "close": close,
            "volume": volume,
            "interval": interval,
            "active_bars": 200,
            "calculation_version": self.main.RSI_CALCULATION_VERSION,
        }

    def test_trusted_rsi_holds_when_source_candle_is_unchanged(self):
        runtime = {}
        first = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(32.8),
            "1m",
            observed_at="2026-07-30T12:00:01+00:00",
        )
        second = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(52.24),
            "1m",
            observed_at="2026-07-30T12:05:01+00:00",
        )

        self.assertEqual(first, ("accepted", 32.8))
        self.assertEqual(second, ("unchanged", 32.8))
        self.assertEqual(runtime["latest_rsi"], 32.8)
        self.assertEqual(runtime["rsi_last_success_at"], "2026-07-30T12:05:01+00:00")

    def test_trusted_rsi_accepts_changed_or_newer_trade_candle(self):
        runtime = {}
        self.main.apply_trusted_rsi_result(runtime, self.rsi_result(32.8), "1m")

        changed = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(40.0, close=10.5, volume=7.0),
            "1m",
        )
        newer = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(48.0, timestamp="2026-07-30T12:01:00+00:00", close=11.0, volume=2.0),
            "1m",
        )

        self.assertEqual(changed, ("accepted", 40.0))
        self.assertEqual(newer, ("accepted", 48.0))
        self.assertEqual(runtime["latest_rsi"], 48.0)

    def test_trusted_rsi_rejects_regressed_candle_and_wrong_interval(self):
        runtime = {}
        self.main.apply_trusted_rsi_result(runtime, self.rsi_result(48.0), "1m")

        regressed = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(20.0, timestamp="2026-07-30T11:59:00+00:00", close=8.0),
            "1m",
        )

        self.assertEqual(regressed, ("regressed", 48.0))
        self.assertEqual(runtime["latest_rsi"], 48.0)
        self.assertEqual(runtime["rsi_status"], "stale")
        with self.assertRaisesRegex(ValueError, "interval"):
            self.main.apply_trusted_rsi_result(runtime, self.rsi_result(50.0, interval="5m"), "1m")

        mismatched_time = list(self.rsi_result(50.0))
        mismatched_time[2] = dict(mismatched_time[2], timestamp="2026-07-30T12:01:00+00:00")
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.main.apply_trusted_rsi_result(runtime, tuple(mismatched_time), "1m")

    def test_trusted_rsi_migrates_existing_same_candle_and_replaces_legacy_value(self):
        runtime = {
            "latest_rsi": 35.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_source": {},
        }

        decision = self.main.apply_trusted_rsi_result(runtime, self.rsi_result(60.0), "1m")

        self.assertEqual(decision, ("accepted", 60.0))
        self.assertEqual(runtime["latest_rsi"], 60.0)
        self.assertEqual(runtime["rsi_source"]["close"], 10.0)
        self.assertEqual(runtime["rsi_value_calculation_version"], self.main.RSI_CALCULATION_VERSION)
        self.assertFalse(runtime["rsi_migration_pending"])

    def test_trusted_rsi_migrates_v337_source_metadata_without_trusting_legacy_value(self):
        runtime = {
            "latest_rsi": 35.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_source": dict(self.rsi_result(35.0)[2]),
        }

        decision = self.main.apply_trusted_rsi_result(runtime, self.rsi_result(60.0), "1m")

        self.assertEqual(decision, ("accepted", 60.0))
        self.assertEqual(runtime["latest_rsi"], 60.0)
        self.assertEqual(runtime["rsi_value_calculation_version"], self.main.RSI_CALCULATION_VERSION)

    def test_trusted_rsi_migration_can_replace_a_newer_legacy_phantom_timestamp(self):
        runtime = {
            "latest_rsi": 35.0,
            "latest_rsi_time": "2026-07-30T13:00:00+00:00",
            "rsi_source": {},
        }

        decision = self.main.apply_trusted_rsi_result(runtime, self.rsi_result(60.0), "1m")

        self.assertEqual(decision, ("accepted", 60.0))
        self.assertEqual(runtime["latest_rsi_time"], "2026-07-30T12:00:00+00:00")

    def test_trusted_rsi_migration_rejects_a_source_older_than_its_v337_watermark(self):
        runtime = {
            "latest_rsi": 35.0,
            "latest_rsi_time": "2026-07-30T13:00:00+00:00",
            "rsi_source": dict(self.rsi_result(35.0)[2]),
        }

        decision = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(20.0, timestamp="2026-07-30T11:59:00+00:00"),
            "1m",
        )

        self.assertEqual(decision, ("regressed", None))
        self.assertEqual(runtime["latest_rsi"], 35.0)
        self.assertTrue(runtime["rsi_migration_pending"])
        self.assertEqual(runtime["rsi_status"], "stale")

    def test_trusted_rsi_requires_current_calculation_metadata(self):
        with self.assertRaisesRegex(ValueError, "calculation metadata"):
            self.main.apply_trusted_rsi_result({}, (60.0, "2026-07-30T12:00:00+00:00"), "1m")

    def test_trusted_rsi_repairs_a_partial_cache_with_no_stored_value(self):
        newer_stamp = "2026-07-30T13:00:00+00:00"
        runtime = {
            "latest_rsi": None,
            "latest_rsi_time": newer_stamp,
            "rsi_source": dict(self.rsi_result(35.0, timestamp=newer_stamp)[2]),
            "rsi_value_calculation_version": self.main.RSI_CALCULATION_VERSION,
        }

        decision = self.main.apply_trusted_rsi_result(
            runtime,
            self.rsi_result(60.0),
            "1m",
        )

        self.assertEqual(decision, ("accepted", 60.0))
        self.assertEqual(runtime["latest_rsi_time"], "2026-07-30T12:00:00+00:00")

    def test_legacy_runtime_load_forces_refresh_and_marks_cached_value_stale(self):
        mint = "TokenLegacyRsi"
        token_state = {
            "latest_rsi": 35.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_status": "ok",
            "rsi_error": None,
            "rsi_last_attempt_at": "2026-07-30T12:05:00+00:00",
            "rsi_source": dict(self.rsi_result(35.0)[2]),
        }
        runtimes_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES.pop(mint, None)
            with mock.patch.object(
                self.main,
                "token_state_from_shared",
                return_value=({"token_states": {mint: token_state}}, token_state),
            ):
                runtime = self.main.get_token_runtime(mint, {"mint": mint})

            self.assertIsNone(runtime["last_rsi_at"])
            self.assertEqual(runtime["rsi_status"], "stale")
            self.assertIn("earlier app version", runtime["rsi_error"])
            self.assertTrue(runtime["rsi_migration_pending"])
            self.assertEqual(runtime["latest_rsi"], 35.0)
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtimes_before)

    def test_current_rsi_runtime_load_keeps_its_normal_schedule(self):
        mint = "TokenCurrentRsi"
        token_state = {
            "latest_rsi": 60.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_status": "ok",
            "rsi_last_attempt_at": "2026-07-30T12:05:00+00:00",
            "rsi_source": dict(self.rsi_result(60.0)[2]),
            "rsi_value_calculation_version": self.main.RSI_CALCULATION_VERSION,
            "rsi_backfill_failures": 2,
            "rsi_backfill_next_attempt_at": "2026-07-31T18:00:00+00:00",
        }
        runtimes_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES.pop(mint, None)
            with mock.patch.object(
                self.main,
                "token_state_from_shared",
                return_value=({"token_states": {mint: token_state}}, token_state),
            ):
                runtime = self.main.get_token_runtime(mint, {"mint": mint})

            self.assertIsNotNone(runtime["last_rsi_at"])
            self.assertEqual(runtime["rsi_status"], "ok")
            self.assertFalse(runtime["rsi_migration_pending"])
            self.assertEqual(runtime["rsi_backfill_failures"], 2)
            self.assertEqual(runtime["rsi_backfill_next_attempt_at"], "2026-07-31T18:00:00+00:00")
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtimes_before)

    def test_active_runtime_restores_persisted_error_without_an_rsi_value(self):
        runtime = {
            "last_rsi_at": datetime.now(timezone.utc),
            "latest_rsi": None,
            "latest_rsi_time": None,
            "rsi_status": "error",
            "rsi_error": "403 Forbidden",
            "rsi_last_fetch_at": "2026-07-30T12:00:00+00:00",
            "rsi_last_attempt_at": "2026-07-30T12:00:00+00:00",
            "rsi_last_success_at": None,
            "rsi_source": {},
            "rsi_refresh_nonce": "same-refresh",
        }
        with mock.patch.multiple(
            self.main,
            load_dynamic_config=mock.Mock(),
            get_token_runtime=mock.Mock(return_value=runtime),
            resolve_token_decimals=mock.Mock(return_value=6),
            get_out_amount_raw=mock.Mock(return_value=100_000_000),
            assess_price_sample=mock.Mock(return_value={"status": "accepted", "reason": "normal"}),
            write_status_json=mock.Mock(return_value="2026-07-30T12:00:00+00:00"),
            OUTPUT_MINT=SOL_MINT,
            ACTIVE_TOKEN_CONFIG={"mint": SOL_MINT, "rsi_refresh_nonce": "same-refresh"},
            BUY_ALERTS=[],
            SELL_ALERTS=[],
            USD_AMOUNT=100.0,
            LATEST_RSI=None,
            LATEST_RSI_TIME=None,
            LATEST_RSI_STATUS="waiting",
            LATEST_RSI_ERROR=None,
            LATEST_RSI_LAST_FETCH_AT=None,
            LATEST_RSI_SOURCE={},
            LATEST_RSI_LAST_ATTEMPT_AT=None,
            LATEST_RSI_LAST_SUCCESS_AT=None,
        ), mock.patch.object(self.main.requests, "post", return_value=mock.Mock(ok=True)):
            self.main.check_prices(price_only=True)

            self.assertEqual(self.main.LATEST_RSI_STATUS, "error")
            self.assertEqual(self.main.LATEST_RSI_ERROR, "403 Forbidden")
            self.assertEqual(self.main.LATEST_RSI_LAST_ATTEMPT_AT, "2026-07-30T12:00:00+00:00")

    def test_active_rsi_backfill_cooldown_keeps_normal_check_but_blocks_extra_history(self):
        trusted_time = "2026-07-20T12:00:00+00:00"
        candidate = dict(self.rsi_result(20.0, "2026-07-24T12:00:00+00:00", close=50.0, volume=2.0)[2])
        due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        runtime = {
            "last_rsi_at": None,
            "latest_rsi": 49.0,
            "latest_rsi_time": trusted_time,
            "rsi_status": "waiting",
            "rsi_error": "history pending",
            "rsi_source": dict(self.rsi_result(49.0, trusted_time, close=114.0, volume=1.0)[2]),
            "rsi_pending_source": candidate,
            "rsi_backfill_failures": 1,
            "rsi_backfill_next_attempt_at": due,
            "rsi_value_calculation_version": self.main.RSI_CALCULATION_VERSION,
            "rsi_migration_pending": False,
            "rsi_refresh_nonce": "same-refresh",
        }
        deferred = self.main.RsiBackfillPendingError(
            "New traded RSI candle detected; waiting for the next protected history retry",
            candidate,
        )
        fetch = mock.Mock(side_effect=deferred)

        with mock.patch.multiple(
            self.main,
            load_dynamic_config=mock.Mock(),
            get_token_runtime=mock.Mock(return_value=runtime),
            resolve_token_decimals=mock.Mock(return_value=6),
            get_out_amount_raw=mock.Mock(return_value=100_000_000),
            assess_price_sample=mock.Mock(return_value={"status": "accepted", "reason": "normal"}),
            get_latest_rsi=fetch,
            write_status_json=mock.Mock(return_value="2026-07-31T12:00:00+00:00"),
            OUTPUT_MINT=SOL_MINT,
            ACTIVE_TOKEN_CONFIG={"mint": SOL_MINT, "rsi_enabled": True, "rsi_refresh_nonce": "same-refresh"},
            BUY_ALERTS=[],
            SELL_ALERTS=[],
            RSI_STATE={},
            RSI_ENABLED=True,
            RSI_INTERVAL="1m",
            RSI_CHECK_INTERVAL=5,
            SOLANATRACKER_API_KEY="key",
            SOLANATRACKER_FEATURES_ENABLED=True,
            USD_AMOUNT=100.0,
        ), mock.patch.object(self.main.requests, "post", return_value=mock.Mock(ok=True)):
            self.main.check_prices()

            fetch.assert_called_once()
            self.assertFalse(fetch.call_args.kwargs["allow_backfill"])
            self.assertEqual(runtime["rsi_backfill_failures"], 1)
            self.assertEqual(runtime["rsi_backfill_next_attempt_at"], due)
            self.assertEqual(runtime["rsi_status"], "waiting")
            self.assertEqual(self.main.LATEST_RSI_STATUS, "waiting")

    def test_rsi_refresh_change_cannot_restore_stale_active_globals(self):
        runtime = {
            "last_rsi_at": datetime.now(timezone.utc),
            "latest_rsi": 72.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_status": "ok",
            "rsi_error": None,
            "rsi_last_fetch_at": "2026-07-30T12:00:01+00:00",
            "rsi_last_attempt_at": "2026-07-30T12:00:01+00:00",
            "rsi_last_success_at": "2026-07-30T12:00:01+00:00",
            "rsi_source": {"timestamp": "2026-07-30T12:00:00+00:00"},
            "rsi_pending_source": dict(self.rsi_result(65.0)[2]),
            "rsi_backfill_failures": 3,
            "rsi_backfill_next_attempt_at": "2026-07-31T18:00:00+00:00",
            "rsi_refresh_nonce": "old-refresh",
        }
        with mock.patch.multiple(
            self.main,
            load_dynamic_config=mock.Mock(),
            get_token_runtime=mock.Mock(return_value=runtime),
            resolve_token_decimals=mock.Mock(return_value=6),
            get_out_amount_raw=mock.Mock(return_value=100_000_000),
            assess_price_sample=mock.Mock(return_value={"status": "accepted", "reason": "normal"}),
            write_status_json=mock.Mock(return_value="2026-07-30T12:00:00+00:00"),
            OUTPUT_MINT=SOL_MINT,
            ACTIVE_TOKEN_CONFIG={"mint": SOL_MINT, "rsi_refresh_nonce": "new-refresh"},
            BUY_ALERTS=[],
            SELL_ALERTS=[],
            USD_AMOUNT=100.0,
            LATEST_RSI=72.0,
            LATEST_RSI_TIME="2026-07-30T12:00:00+00:00",
            LATEST_RSI_STATUS="ok",
            LATEST_RSI_ERROR=None,
            LATEST_RSI_LAST_FETCH_AT="2026-07-30T12:00:01+00:00",
            LATEST_RSI_SOURCE={"timestamp": "2026-07-30T12:00:00+00:00"},
            LATEST_RSI_PENDING_SOURCE=dict(self.rsi_result(65.0)[2]),
            LATEST_RSI_BACKFILL_FAILURES=3,
            LATEST_RSI_BACKFILL_NEXT_ATTEMPT_AT="2026-07-31T18:00:00+00:00",
            LATEST_RSI_LAST_ATTEMPT_AT="2026-07-30T12:00:01+00:00",
            LATEST_RSI_LAST_SUCCESS_AT="2026-07-30T12:00:01+00:00",
        ), mock.patch.object(self.main.requests, "post", return_value=mock.Mock(ok=True)):
            self.main.check_prices(price_only=True)

            self.assertEqual(runtime["rsi_refresh_nonce"], "new-refresh")
            self.assertIsNone(runtime["latest_rsi"])
            self.assertEqual(runtime["rsi_source"], {})
            self.assertEqual(runtime["rsi_pending_source"], {})
            self.assertEqual(runtime["rsi_backfill_failures"], 0)
            self.assertIsNone(runtime["rsi_backfill_next_attempt_at"])
            self.assertEqual(runtime["rsi_status"], "waiting")
            self.assertIsNone(self.main.LATEST_RSI)
            self.assertEqual(self.main.LATEST_RSI_SOURCE, {})
            self.assertEqual(self.main.LATEST_RSI_PENDING_SOURCE, {})
            self.assertEqual(self.main.LATEST_RSI_BACKFILL_FAILURES, 0)
            self.assertIsNone(self.main.LATEST_RSI_BACKFILL_NEXT_ATTEMPT_AT)
            self.assertEqual(self.main.LATEST_RSI_STATUS, "waiting")

    def test_scheduler_picks_one_due_token_round_robin(self):
        cfg = {"check_interval": 60, "rsi_check_interval": 5}
        tokens = [
            {"mint": "TokenA", "enabled": True},
            {"mint": "TokenB", "enabled": True, "check_interval": 10},
        ]

        first = self.main.pick_due_scheduler_token(tokens, cfg)
        self.assertEqual(first["mint"], "TokenA")

        self.main.SCHEDULER_LAST_CHECK["TokenA"] = self.main.time.time()
        second = self.main.pick_due_scheduler_token(tokens, cfg)
        self.assertEqual(second["mint"], "TokenB")

    def test_scheduler_restart_uses_last_real_check_timestamp(self):
        original_read = self.main.read_json_file
        checked_at = "2026-07-28T12:00:00+00:00"
        try:
            self.main.read_json_file = lambda path: {
                "token_states": {
                    "TokenA": {
                        "timestamp": "2026-07-28T12:30:00+00:00",
                        "last_checked_at": checked_at,
                    }
                }
            }

            restored = self.main.scheduler_last_check_for("TokenA")

            self.assertEqual(restored, self.main.parse_iso_to_utc(checked_at).timestamp())
        finally:
            self.main.read_json_file = original_read

    def test_explicit_all_disabled_config_schedules_no_tokens(self):
        tokens = self.main.get_enabled_token_configs({
            "tokens": [
                {"mint": "TokenA", "enabled": False},
                {"mint": "TokenB", "enabled": False},
            ]
        })

        self.assertEqual(tokens, [])
        self.assertIsNone(self.main.pick_due_scheduler_token(tokens, {"check_interval": 60}))
        self.assertEqual(self.main.scheduler_sleep_seconds(tokens, {"check_interval": 60}), 30)

    def test_background_token_never_uses_active_tokens_legacy_history_as_baseline(self):
        now = datetime.now(timezone.utc).isoformat()
        active_history = [
            {"timestamp": now, "buy_price": 1.0, "sell_price": 0.9},
            {"timestamp": now, "buy_price": 1.01, "sell_price": 0.91},
            {"timestamp": now, "buy_price": 0.99, "sell_price": 0.89},
        ]
        with mock.patch.object(
            self.main,
            "token_state_from_shared",
            return_value=({"active_token_mint": "ActiveToken", "latest_prices": active_history}, {}),
        ):
            baseline = self.main.recent_price_baseline("BackgroundToken")

        self.assertEqual(baseline, {})
    def test_price_confirmation_threshold_and_tolerance_boundaries_are_inclusive(self):
        with mock.patch.multiple(
            self.main,
            PRICE_MOVE_CONFIRMATION_THRESHOLD_PERCENT=50.0,
            PRICE_MOVE_CONFIRMATION_TOLERANCE_PERCENT=15.0,
        ):
            self.assertTrue(self.main.price_sample_is_extreme({"buy": 0.5}, {"buy": 1.0}))
            self.assertTrue(self.main.price_sample_is_extreme({"buy": 1.5}, {"buy": 1.0}))
            self.assertFalse(self.main.price_sample_is_extreme({"buy": 0.5001}, {"buy": 1.0}))
            self.assertTrue(self.main.price_samples_are_close({"buy": 100.0}, {"buy": 115.0}))
            self.assertFalse(self.main.price_samples_are_close({"buy": 100.0}, {"buy": 115.01}))
    def test_extreme_price_sample_recovers_without_accepting_the_outlier(self):
        token = {"mint": "TokenMove", "runtime_nonce": "generation-a"}
        with mock.patch.object(self.main, "recent_price_baseline", return_value={"buy": 1.0, "sell": 0.9}):
            first = self.main.assess_price_sample(token, 0.1, 0.09, now=100.0)
            second = self.main.assess_price_sample(token, 0.99, 0.89, now=105.0)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["due_at"], 105.0)
        self.assertEqual(second, {"status": "accepted", "reason": "recovered"})
        self.assertNotIn(token["mint"], self.main.PRICE_CONFIRMATIONS)

    def test_two_similar_extreme_samples_confirm_a_real_move(self):
        token = {"mint": "TokenConfirmedMove", "runtime_nonce": "generation-a"}
        with mock.patch.object(self.main, "recent_price_baseline", return_value={"buy": 1.0, "sell": 0.9}):
            first = self.main.assess_price_sample(token, 0.1, 0.09, now=100.0)
            second = self.main.assess_price_sample(token, 0.105, 0.094, now=105.0)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second, {"status": "accepted", "reason": "confirmed"})
        self.assertNotIn(token["mint"], self.main.PRICE_CONFIRMATIONS)

    def test_confirmation_is_bounded_when_follow_up_quotes_are_unavailable(self):
        token = {"mint": "TokenAmbiguousMove", "runtime_nonce": "generation-a"}
        with mock.patch.object(self.main, "recent_price_baseline", return_value={"buy": 1.0, "sell": 0.9}):
            first = self.main.assess_price_sample(token, 0.1, 0.09, now=100.0)
            second = self.main.assess_price_sample(token, None, None, now=105.0)
            third = self.main.assess_price_sample(token, None, None, now=110.0)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(third["status"], "rejected")
        self.assertNotIn(token["mint"], self.main.PRICE_CONFIRMATIONS)

    def test_changed_token_generation_invalidates_pending_confirmation(self):
        original = {"mint": "TokenReaddedMove", "runtime_nonce": "old"}
        replacement = {"mint": "TokenReaddedMove", "runtime_nonce": "new"}
        with mock.patch.object(self.main, "recent_price_baseline", return_value={"buy": 1.0, "sell": 0.9}):
            self.main.assess_price_sample(original, 0.1, 0.09, now=100.0)

        self.assertIsNone(self.main.valid_price_confirmation(replacement))
        self.assertNotIn(original["mint"], self.main.PRICE_CONFIRMATIONS)

    def test_quote_amount_change_invalidates_pending_confirmation(self):
        original = {"mint": "TokenSettingsMove", "runtime_nonce": "same", "usd_amount": 100}
        changed = {"mint": "TokenSettingsMove", "runtime_nonce": "same", "usd_amount": 250}
        with mock.patch.object(self.main, "recent_price_baseline", return_value={"buy": 1.0, "sell": 0.9}):
            self.main.assess_price_sample(original, 0.1, 0.09, now=100.0)
        self.main.SCHEDULER_LAST_CHECK[original["mint"]] = 100.0

        self.assertIsNone(self.main.valid_price_confirmation(changed))
        self.assertNotIn(original["mint"], self.main.PRICE_CONFIRMATIONS)
        self.assertNotIn(original["mint"], self.main.SCHEDULER_LAST_CHECK)

    def test_scheduler_sleep_uses_pending_confirmation_deadline(self):
        token = {"mint": "TokenSleepMove", "runtime_nonce": "sleep"}
        self.main.PRICE_CONFIRMATIONS[token["mint"]] = {
            "signature": self.main.price_confirmation_signature(token),
            "due_at": 107.0,
            "other_token_needed": False,
            "samples": [],
            "attempts": 1,
        }
        with mock.patch.object(self.main.time, "time", return_value=100.0):
            sleep_for = self.main.scheduler_sleep_seconds([token], {"check_interval": 60})

        self.assertEqual(sleep_for, 7.0)
    def test_due_confirmation_gives_one_other_due_token_a_turn(self):
        tokens = [
            {"mint": "TokenPending", "runtime_nonce": "pending"},
            {"mint": "TokenOther", "runtime_nonce": "other"},
        ]
        pending = tokens[0]
        self.main.PRICE_CONFIRMATIONS[pending["mint"]] = {
            "signature": self.main.price_confirmation_signature(pending),
            "due_at": 90.0,
            "other_token_needed": True,
            "samples": [],
            "attempts": 1,
        }
        with mock.patch.object(self.main.time, "time", return_value=100.0), mock.patch.object(
            self.main, "scheduler_last_check_for", return_value=0.0
        ):
            other = self.main.pick_due_scheduler_token(tokens, {"check_interval": 60})
            self.main.note_scheduler_token_processed(other["mint"])
            confirmation = self.main.pick_due_scheduler_token(tokens, {"check_interval": 60})

        self.assertEqual(other["mint"], "TokenOther")
        self.assertFalse(other.get("_price_confirmation", False))
        self.assertEqual(confirmation["mint"], "TokenPending")
        self.assertTrue(confirmation["_price_confirmation"])

    def test_single_token_confirmation_does_not_wait_for_a_nonexistent_peer(self):
        token = {"mint": "OnlyToken", "runtime_nonce": "only"}
        self.main.PRICE_CONFIRMATIONS[token["mint"]] = {
            "signature": self.main.price_confirmation_signature(token),
            "due_at": 90.0,
            "other_token_needed": True,
            "samples": [],
            "attempts": 1,
        }
        with mock.patch.object(self.main.time, "time", return_value=100.0):
            selected = self.main.pick_due_scheduler_token([token], {"check_interval": 60})

        self.assertEqual(selected["mint"], token["mint"])
        self.assertTrue(selected["_price_confirmation"])

    def test_suspicious_scheduled_quote_cannot_alert_or_write_history_until_confirmed(self):
        token = {
            "mint": "TokenIntegratedMove",
            "name": "Integrated move",
            "runtime_nonce": "move-generation",
            "buy_alerts": [0.5],
            "sell_alerts": [0.05],
            "rsi_enabled": True,
        }
        cfg = {"check_interval": 60, "rsi_check_interval": 5, "rsi_interval": "1m", "rsi_enabled": True}
        send = mock.Mock(return_value=True)
        full_writer = mock.Mock(return_value=True)
        marker_writer = mock.Mock(return_value=True)
        rsi = mock.Mock(return_value=self.rsi_result(50.0, "2026-07-29T12:00:00+00:00"))
        quote_outputs = iter([1_000_000_000, 100_000_000, 1_050_000_000, 105_000_000])

        def staged_quote(input_mint, output_mint, amount, cache_result=True, quote_capture=None):
            out_amount = next(quote_outputs)
            key = (str(input_mint), str(output_mint), int(amount))
            entry = {"quote": {"outAmount": str(out_amount)}, "fetched_at": "2026-07-29T12:00:00+00:00"}
            if cache_result:
                self.main.QUOTE_CACHE[key] = entry
            if isinstance(quote_capture, dict):
                quote_capture[key] = entry
            return out_amount

        runtime_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES.pop(token["mint"], None)
            with mock.patch.multiple(
                self.main,
                SOLANATRACKER_API_KEY="key",
                SOLANATRACKER_FEATURES_ENABLED=True,
                resolve_token_decimals=mock.Mock(return_value=6),
                get_out_amount_raw=mock.Mock(side_effect=staged_quote),
                get_latest_rsi=rsi,
                recent_price_baseline=mock.Mock(return_value={"buy": 1.0, "sell": 0.9}),
                send_alert=send,
                write_scheduled_token_status=full_writer,
                write_price_verification_state=marker_writer,
            ):
                first = self.main.check_scheduled_token(token, cfg)
                self.assertEqual(first["status"], "pending")
                send.assert_not_called()
                full_writer.assert_not_called()
                marker_writer.assert_called_once()
                self.assertEqual(rsi.call_count, 1)
                self.assertEqual(self.main.QUOTE_CACHE, {})

                second = self.main.check_scheduled_token(token, cfg, price_only=True)

            self.assertEqual(second, {"status": "accepted", "reason": "confirmed"})
            self.assertEqual(send.call_count, 2)
            full_writer.assert_called_once()
            self.assertEqual(rsi.call_count, 1)
            self.assertEqual(len(self.main.QUOTE_CACHE), 2)
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtime_before)

    def test_verification_marker_preserves_last_trusted_price_and_history(self):
        mint = "TokenPersistedMove"
        token = {"mint": mint, "runtime_nonce": "persisted-generation"}
        existing = {
            "token_states": {
                mint: {
                    "runtime_nonce": "persisted-generation",
                    "buy_price": 1.0,
                    "sell_price": 0.9,
                    "last_checked_at": "2026-07-29T11:00:00+00:00",
                }
            },
            "token_price_history": {mint: [{"timestamp": "2026-07-29T11:00:00+00:00", "buy_price": 1.0, "sell_price": 0.9}]},
        }
        captured = {}

        class DummyLock:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch.multiple(
            self.main,
            read_json_file=mock.Mock(return_value=copy.deepcopy(existing)),
            atomic_write_json=mock.Mock(side_effect=lambda _path, data: captured.update(data=copy.deepcopy(data))),
            json_file_lock=mock.Mock(side_effect=lambda _path: DummyLock()),
        ):
            wrote = self.main.write_price_verification_state(
                token,
                "verifying",
                "Verifying an unusual Jupiter price move",
                due_at=100.0,
                runtime={
                    "rsi_backfill_failures": 2,
                    "rsi_backfill_next_attempt_at": "2026-07-31T18:00:00+00:00",
                },
            )

        state = captured["data"]["token_states"][mint]
        self.assertTrue(wrote)
        self.assertEqual(state["buy_price"], 1.0)
        self.assertEqual(state["sell_price"], 0.9)
        self.assertEqual(state["last_checked_at"], "2026-07-29T11:00:00+00:00")
        self.assertEqual(captured["data"]["token_price_history"], existing["token_price_history"])
        self.assertEqual(state["price_status"], "verifying")
        self.assertEqual(state["rsi_backfill_failures"], 2)
        self.assertEqual(state["rsi_backfill_next_attempt_at"], "2026-07-31T18:00:00+00:00")
        self.assertIsNone(state["error"])
    def test_active_switch_invalidates_alert_runtime_without_moving_schedule(self):
        previous_mint = "TokenPrevious"
        next_mint = "TokenNext"
        originals = {
            "output_mint": self.main.OUTPUT_MINT,
            "runtimes": copy.deepcopy(self.main.TOKEN_RUNTIMES),
            "scheduler": dict(self.main.SCHEDULER_LAST_CHECK),
            "last_buy": dict(self.main.last_buy_alert),
            "last_sell": dict(self.main.last_sell_alert),
            "rsi_state": copy.deepcopy(self.main.RSI_STATE),
            "last_rsi_at": self.main._last_rsi_at,
            "latest_rsi": self.main.LATEST_RSI,
            "latest_rsi_time": self.main.LATEST_RSI_TIME,
            "latest_rsi_status": self.main.LATEST_RSI_STATUS,
            "latest_rsi_error": self.main.LATEST_RSI_ERROR,
            "latest_rsi_last_fetch_at": self.main.LATEST_RSI_LAST_FETCH_AT,
            "token_changed": self.main.TOKEN_CHANGED_SINCE_LAST_WRITE,
        }
        try:
            self.main.OUTPUT_MINT = previous_mint
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update({previous_mint: {"runtime_nonce": "a"}, next_mint: {"runtime_nonce": "b"}})
            self.main.SCHEDULER_LAST_CHECK.clear()
            self.main.SCHEDULER_LAST_CHECK.update({previous_mint: 100.0, next_mint: 200.0})

            changed = self.main.reset_active_token_runtime(next_mint)

            self.assertTrue(changed)
            self.assertNotIn(previous_mint, self.main.TOKEN_RUNTIMES)
            self.assertNotIn(next_mint, self.main.TOKEN_RUNTIMES)
            self.assertEqual(self.main.SCHEDULER_LAST_CHECK, {previous_mint: 100.0, next_mint: 200.0})
        finally:
            self.main.OUTPUT_MINT = originals["output_mint"]
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(originals["runtimes"])
            self.main.SCHEDULER_LAST_CHECK.clear()
            self.main.SCHEDULER_LAST_CHECK.update(originals["scheduler"])
            self.main.last_buy_alert.clear()
            self.main.last_buy_alert.update(originals["last_buy"])
            self.main.last_sell_alert.clear()
            self.main.last_sell_alert.update(originals["last_sell"])
            self.main.RSI_STATE.clear()
            self.main.RSI_STATE.update(originals["rsi_state"])
            self.main._last_rsi_at = originals["last_rsi_at"]
            self.main.LATEST_RSI = originals["latest_rsi"]
            self.main.LATEST_RSI_TIME = originals["latest_rsi_time"]
            self.main.LATEST_RSI_STATUS = originals["latest_rsi_status"]
            self.main.LATEST_RSI_ERROR = originals["latest_rsi_error"]
            self.main.LATEST_RSI_LAST_FETCH_AT = originals["latest_rsi_last_fetch_at"]
            self.main.TOKEN_CHANGED_SINCE_LAST_WRITE = originals["token_changed"]

    def test_readded_token_nonce_invalidates_alert_and_scheduler_caches(self):
        mint = "TokenReadded"
        stale_key = "1.00000000"
        original_runtime = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        original_reader = self.main.token_state_from_shared
        try:
            self.main.TOKEN_RUNTIMES[mint] = {
                "runtime_nonce": "old-generation",
                "last_buy_alert": {stale_key: datetime.now(timezone.utc)},
            }
            self.main.SCHEDULER_LAST_CHECK[mint] = 123.0
            self.main.SCHEDULER_TOKEN_NONCES[mint] = "old-generation"
            stale_checked_at = "2026-07-28T12:00:00+00:00"
            self.main.token_state_from_shared = lambda token_mint: (
                {"last_triggered_buy": {stale_key: stale_checked_at}},
                {
                    "runtime_nonce": "old-generation",
                    "last_triggered_buy": {stale_key: stale_checked_at},
                    "last_checked_at": stale_checked_at,
                },
            )
            readded = {"mint": mint, "runtime_nonce": "new-generation"}

            runtime = self.main.get_token_runtime(mint, readded)
            last_check = self.main.scheduler_last_check_for(mint, readded)

            self.assertEqual(runtime["runtime_nonce"], "new-generation")
            self.assertNotIn(stale_key, runtime["last_buy_alert"])
            self.assertEqual(last_check, 0.0)
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(original_runtime)
            self.main.token_state_from_shared = original_reader

    def test_stale_inflight_write_cannot_overwrite_readded_token(self):
        mint = "TokenReaddedDuringCheck"
        store = {
            "token_states": {
                mint: {
                    "runtime_nonce": "new-generation",
                    "last_triggered_buy": {},
                }
            },
            "token_price_history": {},
        }
        writer = mock.Mock()

        class DummyLock:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch.multiple(
            self.main,
            read_json_file=mock.Mock(side_effect=lambda _path: copy.deepcopy(store)),
            atomic_write_json=writer,
            json_file_lock=mock.Mock(side_effect=lambda _path: DummyLock()),
        ):
            wrote = self.main.write_scheduled_token_status(
                {"mint": mint, "name": "Old generation", "runtime_nonce": "old-generation"},
                {
                    "last_buy_alert": {"1.00000000": datetime.now(timezone.utc)},
                    "last_sell_alert": {},
                    "last_triggered_rsi": {},
                },
                1.0,
                1.0,
                100.0,
                100.0,
                60,
                5,
                rsi_enabled=False,
            )

        self.assertFalse(wrote)
        writer.assert_not_called()
        self.assertEqual(store["token_states"][mint]["runtime_nonce"], "new-generation")
        self.assertEqual(store["token_states"][mint]["last_triggered_buy"], {})

    def test_stale_verification_write_cannot_overwrite_readded_token(self):
        mint = "TokenReaddedDuringVerification"
        existing = {"token_states": {mint: {"runtime_nonce": "new-generation", "buy_price": 1.0}}}
        writer = mock.Mock()

        class DummyLock:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch.multiple(
            self.main,
            read_json_file=mock.Mock(return_value=copy.deepcopy(existing)),
            atomic_write_json=writer,
            json_file_lock=mock.Mock(side_effect=lambda _path: DummyLock()),
        ):
            wrote = self.main.write_price_verification_state(
                {"mint": mint, "runtime_nonce": "old-generation"},
                "verifying",
                "Verifying an unusual Jupiter price move",
                due_at=100.0,
            )

        self.assertFalse(wrote)
        writer.assert_not_called()
    def test_price_quote_can_be_staged_without_leaking_into_shared_cache(self):
        capture = {}
        with mock.patch.object(self.main, "get_quote", return_value={"outAmount": "12345"}):
            out_amount = self.main.get_out_amount_raw(
                "InputMint",
                "OutputMint",
                1000,
                cache_result=False,
                quote_capture=capture,
            )

        key = ("InputMint", "OutputMint", 1000)
        self.assertEqual(out_amount, 12345)
        self.assertNotIn(key, self.main.QUOTE_CACHE)
        self.assertEqual(capture[key]["quote"]["outAmount"], "12345")

    def test_disabled_pending_token_is_removed_and_rechecks_immediately_if_reenabled(self):
        mint = "TokenTemporarilyDisabled"
        token = {"mint": mint, "runtime_nonce": "same"}
        self.main.PRICE_CONFIRMATIONS[mint] = {
            "signature": self.main.price_confirmation_signature(token),
            "due_at": 100.0,
            "other_token_needed": True,
            "samples": [],
            "attempts": 1,
        }
        self.main.SCHEDULER_LAST_CHECK[mint] = 100.0

        self.assertIsNone(self.main.pick_due_scheduler_token([], {"check_interval": 60}))
        self.assertNotIn(mint, self.main.PRICE_CONFIRMATIONS)
        self.assertNotIn(mint, self.main.SCHEDULER_LAST_CHECK)
    def test_rate_limit_mode_off_bypasses_limiter(self):
        self.main.SOLANATRACKER_RATE_LIMIT_MODE = "off"
        self.main.SOLANATRACKER_REQUESTS_PER_SECOND = 50
        self.main.apply_solanatracker_rate_limit()
        self.assertFalse(self.limiter.is_rate_limit_enabled())

        self.main.SOLANATRACKER_RATE_LIMIT_MODE = "custom"
        self.main.SOLANATRACKER_REQUESTS_PER_SECOND = 5
        self.main.apply_solanatracker_rate_limit()
        self.assertTrue(self.limiter.is_rate_limit_enabled())
        self.assertEqual(self.main.effective_rate_limit_rps("custom", 5), 5)
        self.assertEqual(self.main.effective_rate_limit_rps("safe", 5), 1.0)



    def test_token_rsi_enabled_prefers_token_override(self):
        original_rsi_enabled = self.main.RSI_ENABLED
        try:
            self.main.RSI_ENABLED = True
            self.assertTrue(self.main.token_rsi_enabled({}, {"rsi_enabled": True}))
            self.assertFalse(self.main.token_rsi_enabled({"rsi_enabled": False}, {"rsi_enabled": True}))
            self.assertFalse(self.main.token_rsi_enabled({}, {"rsi_enabled": False}))
            self.assertTrue(self.main.token_rsi_enabled({"rsi_enabled": True}, {"rsi_enabled": False}))
        finally:
            self.main.RSI_ENABLED = original_rsi_enabled

    def test_scheduled_token_uses_per_token_rsi_enabled(self):
        token = {"mint": "TokenNoRsi", "name": "No RSI", "rsi_enabled": False}
        cfg = {"rsi_enabled": True, "rsi_check_interval": 5, "check_interval": 60}
        captured = {}
        rsi_calls = {"count": 0}

        originals = {
            "solanatracker_api_key": self.main.SOLANATRACKER_API_KEY,
            "solanatracker_features_enabled": self.main.SOLANATRACKER_FEATURES_ENABLED,
            "rsi_enabled": self.main.RSI_ENABLED,
            "resolve_token_decimals": self.main.resolve_token_decimals,
            "get_out_amount_raw": self.main.get_out_amount_raw,
            "get_latest_rsi": self.main.get_latest_rsi,
            "write_scheduled_token_status": self.main.write_scheduled_token_status,
            "send_alert": self.main.send_alert,
            "runtime": copy.deepcopy(self.main.TOKEN_RUNTIMES),
        }

        def fake_get_latest_rsi(*args, **kwargs):
            rsi_calls["count"] += 1
            raise AssertionError("RSI should not be fetched when token RSI is disabled")

        try:
            self.main.SOLANATRACKER_API_KEY = "key"
            self.main.SOLANATRACKER_FEATURES_ENABLED = True
            self.main.RSI_ENABLED = True
            self.main.resolve_token_decimals = lambda mint, configured_decimals=None: 6
            self.main.get_out_amount_raw = lambda *args, **kwargs: 1_000_000
            self.main.get_latest_rsi = fake_get_latest_rsi
            self.main.send_alert = lambda *args, **kwargs: None
            self.main.write_scheduled_token_status = lambda *args, **kwargs: captured.update(kwargs)

            self.main.check_scheduled_token(token, cfg)

            runtime = self.main.TOKEN_RUNTIMES[token["mint"]]
            self.assertEqual(runtime["rsi_status"], "disabled")
            self.assertEqual(runtime["rsi_error"], "RSI disabled for this token")
            self.assertFalse(captured["rsi_enabled"])
            self.assertEqual(rsi_calls["count"], 0)
        finally:
            self.main.SOLANATRACKER_API_KEY = originals["solanatracker_api_key"]
            self.main.SOLANATRACKER_FEATURES_ENABLED = originals["solanatracker_features_enabled"]
            self.main.RSI_ENABLED = originals["rsi_enabled"]
            self.main.resolve_token_decimals = originals["resolve_token_decimals"]
            self.main.get_out_amount_raw = originals["get_out_amount_raw"]
            self.main.get_latest_rsi = originals["get_latest_rsi"]
            self.main.write_scheduled_token_status = originals["write_scheduled_token_status"]
            self.main.send_alert = originals["send_alert"]
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(originals["runtime"])

    def test_scheduled_price_alerts_continue_when_rsi_is_forbidden(self):
        token = {"mint": "TokenRsi403", "name": "RSI 403", "buy_alerts": [2.0], "rsi_enabled": True}
        cfg = {"rsi_enabled": True, "rsi_check_interval": 5, "check_interval": 60}
        sent_titles = []
        status_writer = mock.Mock()
        runtime_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)

        try:
            self.main.TOKEN_RUNTIMES.pop(token["mint"], None)
            with mock.patch.multiple(
                self.main,
                SOLANATRACKER_API_KEY="key",
                SOLANATRACKER_FEATURES_ENABLED=True,
                RSI_ENABLED=True,
                resolve_token_decimals=mock.Mock(return_value=6),
                get_out_amount_raw=mock.Mock(side_effect=[100_000_000, 100_000_000]),
                get_latest_rsi=mock.Mock(side_effect=RuntimeError("403 Forbidden")),
                send_alert=mock.Mock(side_effect=lambda title, *args, **kwargs: sent_titles.append(title) or True),
                write_scheduled_token_status=status_writer,
            ):
                self.main.check_scheduled_token(token, cfg)

            runtime = self.main.TOKEN_RUNTIMES[token["mint"]]
            self.assertIn("Buy Price Alert", sent_titles)
            self.assertEqual(runtime["rsi_status"], "error")
            self.assertIn("403 Forbidden", runtime["rsi_error"])
            self.assertTrue(status_writer.called)
            self.assertIsNone(status_writer.call_args.kwargs["error"])
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtime_before)

    def test_rsi_backfill_pending_suppresses_old_value_and_alert_until_verified(self):
        trusted_time = "2026-07-20T12:00:00+00:00"
        new_time = "2026-07-24T12:00:00+00:00"
        token = {
            "mint": "TokenLongGapRsi",
            "name": "Long-gap RSI",
            "buy_alerts": [],
            "sell_alerts": [],
            "rsi_alerts": ["below:30"],
            "rsi_enabled": True,
            "runtime_nonce": "long-gap-generation",
        }
        cfg = {"check_interval": 60, "rsi_check_interval": 5, "rsi_interval": "1m", "rsi_enabled": True}
        trusted_source = dict(self.rsi_result(49.0, trusted_time, close=114.0, volume=1.0)[2])
        candidate_source = dict(self.rsi_result(20.0, new_time, close=50.0, volume=2.0)[2])
        runtime = {
            "last_buy_alert": {},
            "last_sell_alert": {},
            "last_triggered_rsi": {},
            "rsi_state": {},
            "last_rsi_at": None,
            "latest_rsi": 49.0,
            "latest_rsi_time": trusted_time,
            "rsi_status": "ok",
            "rsi_error": None,
            "rsi_last_fetch_at": trusted_time,
            "rsi_last_attempt_at": trusted_time,
            "rsi_last_success_at": trusted_time,
            "rsi_source": trusted_source,
            "rsi_pending_source": {},
            "rsi_backfill_failures": 0,
            "rsi_backfill_next_attempt_at": None,
            "rsi_value_calculation_version": self.main.RSI_CALCULATION_VERSION,
            "rsi_migration_pending": False,
            "rsi_refresh_nonce": "",
            "runtime_nonce": "long-gap-generation",
        }
        pending = self.main.RsiBackfillPendingError(
            "New traded RSI candle detected; historical candles are temporarily unavailable",
            candidate_source,
            backfill_attempted=True,
        )
        deferred = self.main.RsiBackfillPendingError(
            "New traded RSI candle detected; waiting for the next protected history retry",
            candidate_source,
        )
        fetch = mock.Mock(side_effect=[
            pending,
            deferred,
            self.rsi_result(20.0, new_time, close=50.0, volume=2.0),
        ])
        sent = mock.Mock(return_value=True)
        runtimes_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES[token["mint"]] = runtime
            with mock.patch.multiple(
                self.main,
                SOLANATRACKER_API_KEY="key",
                SOLANATRACKER_FEATURES_ENABLED=True,
                RSI_ENABLED=True,
                resolve_token_decimals=mock.Mock(return_value=6),
                get_out_amount_raw=mock.Mock(return_value=100_000_000),
                assess_price_sample=mock.Mock(return_value={"status": "accepted", "reason": "normal"}),
                get_latest_rsi=fetch,
                send_alert=sent,
                write_scheduled_token_status=mock.Mock(),
            ):
                self.main.check_scheduled_token(token, cfg)

                sent.assert_not_called()
                self.assertTrue(fetch.call_args_list[0].kwargs["allow_backfill"])
                self.assertEqual(runtime["latest_rsi"], 49.0)
                self.assertEqual(runtime["rsi_status"], "waiting")
                self.assertEqual(runtime["rsi_pending_source"]["timestamp"], new_time)
                self.assertEqual(runtime["rsi_backfill_failures"], 1)
                first_due = runtime["rsi_backfill_next_attempt_at"]
                self.assertGreater(
                    self.main.parse_iso_to_utc(first_due),
                    datetime.now(timezone.utc),
                )
                self.assertNotIn("below:30.00", runtime["last_triggered_rsi"])
                runtime["last_rsi_at"] = None

                self.main.check_scheduled_token(token, cfg)

                sent.assert_not_called()
                self.assertFalse(fetch.call_args_list[1].kwargs["allow_backfill"])
                self.assertEqual(runtime["rsi_backfill_failures"], 1)
                self.assertEqual(runtime["rsi_backfill_next_attempt_at"], first_due)
                runtime["rsi_backfill_next_attempt_at"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat()
                runtime["last_rsi_at"] = None

                self.main.check_scheduled_token(token, cfg)

            self.assertTrue(fetch.call_args_list[2].kwargs["allow_backfill"])
            sent.assert_called_once()
            self.assertEqual(sent.call_args.args[0], "RSI Alert")
            self.assertEqual(runtime["latest_rsi"], 20.0)
            self.assertEqual(runtime["rsi_pending_source"], {})
            self.assertEqual(runtime["rsi_backfill_failures"], 0)
            self.assertIsNone(runtime["rsi_backfill_next_attempt_at"])
            self.assertEqual(runtime["rsi_status"], "ok")
            self.assertIn("below:30.00", runtime["last_triggered_rsi"])
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtimes_before)

    def test_rsi_backfill_retry_delays_escalate_and_reset(self):
        candidate = dict(self.rsi_result(20.0, "2026-07-24T12:00:00+00:00")[2])
        runtime = {}
        base = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        observed_times = [base, base + timedelta(hours=2), base + timedelta(hours=9), base + timedelta(hours=34)]
        expected_delays = [timedelta(hours=1), timedelta(hours=6), timedelta(hours=24), timedelta(hours=24)]

        for index, (observed, expected_delay) in enumerate(zip(observed_times, expected_delays), start=1):
            error = self.main.RsiBackfillPendingError("history unavailable", candidate, backfill_attempted=True)
            self.main.mark_rsi_backfill_pending(runtime, error, observed_at=observed.isoformat())
            self.assertEqual(runtime["rsi_backfill_failures"], index)
            self.assertEqual(
                self.main.parse_iso_to_utc(runtime["rsi_backfill_next_attempt_at"]),
                observed + expected_delay,
            )

        saved_due = runtime["rsi_backfill_next_attempt_at"]
        deferred = self.main.RsiBackfillPendingError("protected retry pending", candidate)
        self.main.mark_rsi_backfill_pending(
            runtime,
            deferred,
            observed_at=(base + timedelta(hours=35)).isoformat(),
        )
        self.assertEqual(runtime["rsi_backfill_failures"], 4)
        self.assertEqual(runtime["rsi_backfill_next_attempt_at"], saved_due)
        due = self.main.parse_iso_to_utc(saved_due)
        self.assertFalse(self.main.rsi_backfill_retry_allowed(runtime, due - timedelta(seconds=1)))
        self.assertTrue(self.main.rsi_backfill_retry_allowed(runtime, due))

        self.main.clear_runtime_rsi(runtime, "waiting")
        self.assertEqual(runtime["rsi_backfill_failures"], 0)
        self.assertIsNone(runtime["rsi_backfill_next_attempt_at"])

        orphaned = {
            "rsi_pending_source": {},
            "rsi_backfill_failures": 3,
            "rsi_backfill_next_attempt_at": (base + timedelta(days=1)).isoformat(),
        }
        self.assertTrue(self.main.rsi_backfill_retry_allowed(orphaned, base))
        self.assertEqual(orphaned["rsi_backfill_failures"], 0)
        self.assertIsNone(orphaned["rsi_backfill_next_attempt_at"])

        malformed_due = {
            "rsi_pending_source": candidate,
            "rsi_backfill_failures": 2,
            "rsi_backfill_next_attempt_at": "not-a-date",
        }
        self.assertTrue(self.main.rsi_backfill_retry_allowed(malformed_due, base))
        self.assertEqual(malformed_due["rsi_backfill_failures"], 0)
        self.assertIsNone(malformed_due["rsi_backfill_next_attempt_at"])

    def test_legacy_rsi_never_fires_alert_and_retries_on_the_next_due_cycle(self):
        token = {
            "mint": "TokenLegacyRetry",
            "name": "Legacy RSI retry",
            "buy_alerts": [],
            "sell_alerts": [],
            "rsi_alerts": ["above:50"],
            "rsi_enabled": True,
            "runtime_nonce": "legacy-rsi-generation",
        }
        cfg = {"check_interval": 60, "rsi_check_interval": 5, "rsi_interval": "1m", "rsi_enabled": True}
        runtime = {
            "last_buy_alert": {},
            "last_sell_alert": {},
            "last_triggered_rsi": {},
            "rsi_state": {},
            "last_rsi_at": None,
            "latest_rsi": 80.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_status": "stale",
            "rsi_error": "RSI was calculated by an earlier app version; waiting for a verified refresh",
            "rsi_last_fetch_at": "2026-07-30T12:00:01+00:00",
            "rsi_last_attempt_at": "2026-07-30T12:00:01+00:00",
            "rsi_last_success_at": "2026-07-30T12:00:01+00:00",
            "rsi_source": dict(self.rsi_result(80.0)[2]),
            "rsi_value_calculation_version": "",
            "rsi_migration_pending": True,
            "rsi_refresh_nonce": "",
            "runtime_nonce": "legacy-rsi-generation",
        }
        sent = mock.Mock(return_value=True)
        fetch = mock.Mock(side_effect=[
            RuntimeError("403 Forbidden"),
            self.rsi_result(60.0),
        ])
        runtimes_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES[token["mint"]] = runtime
            with mock.patch.multiple(
                self.main,
                SOLANATRACKER_API_KEY="key",
                SOLANATRACKER_FEATURES_ENABLED=True,
                RSI_ENABLED=True,
                resolve_token_decimals=mock.Mock(return_value=6),
                get_out_amount_raw=mock.Mock(return_value=100_000_000),
                assess_price_sample=mock.Mock(return_value={"status": "accepted", "reason": "normal"}),
                get_latest_rsi=fetch,
                send_alert=sent,
                write_scheduled_token_status=mock.Mock(),
            ):
                self.main.check_scheduled_token(token, cfg)

                sent.assert_not_called()
                self.assertEqual(runtime["latest_rsi"], 80.0)
                self.assertTrue(runtime["rsi_migration_pending"])
                self.assertEqual(runtime["rsi_status"], "error")
                runtime["last_rsi_at"] = None

                self.main.check_scheduled_token(token, cfg)

            sent.assert_called_once()
            self.assertEqual(runtime["latest_rsi"], 60.0)
            self.assertEqual(runtime["rsi_value_calculation_version"], self.main.RSI_CALCULATION_VERSION)
            self.assertFalse(runtime["rsi_migration_pending"])
            self.assertIn("above:50.00", runtime["last_triggered_rsi"])
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtimes_before)

    def test_failed_price_notification_retries_before_recording_trigger(self):
        token = {
            "mint": "TokenDeliveryRetry",
            "name": "Delivery retry",
            "buy_alerts": [2.0],
            "sell_alerts": [],
            "rsi_enabled": False,
            "runtime_nonce": "delivery-generation",
        }
        cfg = {"check_interval": 60, "rsi_enabled": False}
        send = mock.Mock(side_effect=[False, True])
        runtime_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES.pop(token["mint"], None)
            with mock.patch.multiple(
                self.main,
                resolve_token_decimals=mock.Mock(return_value=6),
                get_out_amount_raw=mock.Mock(return_value=100_000_000),
                send_alert=send,
                write_scheduled_token_status=mock.Mock(),
            ):
                self.main.check_scheduled_token(token, cfg)
                runtime = self.main.TOKEN_RUNTIMES[token["mint"]]
                self.assertNotIn("2.00000000", runtime["last_buy_alert"])

                self.main.check_scheduled_token(token, cfg)

            self.assertEqual(send.call_count, 2)
            self.assertIn("2.00000000", self.main.TOKEN_RUNTIMES[token["mint"]]["last_buy_alert"])
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtime_before)

    def test_failed_rsi_notification_retries_before_recording_trigger(self):
        token = {
            "mint": "TokenRsiDeliveryRetry",
            "name": "RSI delivery retry",
            "buy_alerts": [],
            "sell_alerts": [],
            "rsi_alerts": ["above:50"],
            "rsi_enabled": True,
            "runtime_nonce": "rsi-delivery-generation",
        }
        cfg = {"check_interval": 60, "rsi_check_interval": 5, "rsi_interval": "1m", "rsi_enabled": True}
        send = mock.Mock(side_effect=[False, True])
        runtime_before = copy.deepcopy(self.main.TOKEN_RUNTIMES)
        try:
            self.main.TOKEN_RUNTIMES.pop(token["mint"], None)
            with mock.patch.multiple(
                self.main,
                SOLANATRACKER_API_KEY="key",
                SOLANATRACKER_FEATURES_ENABLED=True,
                RSI_ENABLED=True,
                resolve_token_decimals=mock.Mock(return_value=6),
                get_out_amount_raw=mock.Mock(return_value=100_000_000),
                get_latest_rsi=mock.Mock(return_value=self.rsi_result(80.0, "2026-07-28T16:00:00+00:00")),
                send_alert=send,
                write_scheduled_token_status=mock.Mock(),
            ):
                self.main.check_scheduled_token(token, cfg)
                runtime = self.main.TOKEN_RUNTIMES[token["mint"]]
                self.assertNotIn("above:50.00", runtime["last_triggered_rsi"])
                runtime["last_rsi_at"] = None

                self.main.check_scheduled_token(token, cfg)

            self.assertEqual(send.call_count, 2)
            self.assertIn("above:50.00", self.main.TOKEN_RUNTIMES[token["mint"]]["last_triggered_rsi"])
        finally:
            self.main.TOKEN_RUNTIMES.clear()
            self.main.TOKEN_RUNTIMES.update(runtime_before)

    def test_failed_active_price_notification_retries_before_recording_trigger(self):
        send = mock.Mock(side_effect=[False, True])
        notify = mock.Mock()
        status_writer = mock.Mock(return_value="2026-07-28T16:00:00+00:00")

        with mock.patch.multiple(
            self.main,
            load_dynamic_config=mock.Mock(),
            resolve_token_decimals=mock.Mock(return_value=6),
            get_out_amount_raw=mock.Mock(return_value=100_000_000),
            send_alert=send,
            notify_backend_trigger=notify,
            write_status_json=status_writer,
            USD_AMOUNT=100.0,
            BUY_ALERTS=[2.0],
            SELL_ALERTS=[],
            ALERT_RESET_MINUTES=0,
            INPUT_DECIMALS=6,
            OUTPUT_DECIMALS=6,
            ACTIVE_TOKEN_CONFIG={"mint": SOL_MINT, "rsi_enabled": False},
            last_buy_alert={},
            last_sell_alert={},
            RSI_STATE={},
            _last_rsi_at=None,
            LATEST_RSI=None,
            LATEST_RSI_TIME=None,
            LATEST_RSI_STATUS="disabled",
            LATEST_RSI_ERROR=None,
            LATEST_RSI_LAST_FETCH_AT=None,
        ), mock.patch.object(self.main.requests, "post", return_value=mock.Mock(ok=True)):
            self.main.check_prices()

            self.assertNotIn("2.00000000", self.main.last_buy_alert)
            notify.assert_not_called()

            self.main.check_prices()

            self.assertEqual(send.call_count, 2)
            self.assertIn("2.00000000", self.main.last_buy_alert)
            notify.assert_called_once_with("buy", 2.0)

    def test_suspicious_active_quote_is_not_published_until_confirmed(self):
        send = mock.Mock(return_value=True)
        notify = mock.Mock()
        status_writer = mock.Mock(return_value="2026-07-29T12:00:00+00:00")
        marker_writer = mock.Mock(return_value=True)
        with mock.patch.multiple(
            self.main,
            load_dynamic_config=mock.Mock(),
            resolve_token_decimals=mock.Mock(return_value=6),
            get_out_amount_raw=mock.Mock(side_effect=[1_000_000_000, 100_000_000, 1_050_000_000, 105_000_000]),
            recent_price_baseline=mock.Mock(return_value={"buy": 1.0, "sell": 0.9}),
            send_alert=send,
            notify_backend_trigger=notify,
            write_status_json=status_writer,
            write_price_verification_state=marker_writer,
            OUTPUT_MINT=SOL_MINT,
            USD_AMOUNT=100.0,
            BUY_ALERTS=[0.5],
            SELL_ALERTS=[],
            ALERT_RESET_MINUTES=0,
            INPUT_DECIMALS=6,
            OUTPUT_DECIMALS=6,
            CHECK_INTERVAL=60,
            ACTIVE_TOKEN_CONFIG={"mint": SOL_MINT, "runtime_nonce": "active-move", "rsi_enabled": False},
            last_buy_alert={},
            last_sell_alert={},
            RSI_STATE={},
            _last_rsi_at=None,
            LATEST_RSI=None,
            LATEST_RSI_TIME=None,
            LATEST_RSI_STATUS="disabled",
            LATEST_RSI_ERROR=None,
            LATEST_RSI_LAST_FETCH_AT=None,
        ), mock.patch.object(self.main.requests, "post", return_value=mock.Mock(ok=True)) as post:
            first = self.main.check_prices()
            self.assertEqual(first["status"], "pending")
            send.assert_not_called()
            status_writer.assert_not_called()
            post.assert_not_called()

            second = self.main.check_prices(price_only=True)

        self.assertEqual(second, {"status": "accepted", "reason": "confirmed"})
        send.assert_called_once()
        notify.assert_called_once_with("buy", 0.5)
        completed_writes = [
            call for call in status_writer.call_args_list
            if call.kwargs.get("check_completed", True)
        ]
        self.assertEqual(len(completed_writes), 1)
        post.assert_called_once()
    def test_active_rsi_trigger_persists_when_backend_callback_fails(self):
        trigger_key = "above:50.00"
        trigger_time = "2026-07-28T16:00:00+00:00"
        existing = {
            "token_states": {
                SOL_MINT: {
                    "runtime_nonce": "active-generation",
                    "last_triggered_rsi": {},
                }
            },
            "token_price_history": {},
            "last_triggered_rsi": {},
        }
        captured = {}
        callback = mock.Mock(return_value=False)

        class DummyLock:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch.multiple(
            self.main,
            load_dynamic_config=mock.Mock(),
            resolve_token_decimals=mock.Mock(return_value=6),
            get_out_amount_raw=mock.Mock(return_value=100_000_000),
            get_latest_rsi=mock.Mock(return_value=self.rsi_result(80.0, trigger_time)),
            send_alert=mock.Mock(return_value=True),
            notify_backend_rsi_trigger=callback,
            read_json_file=mock.Mock(side_effect=lambda _path: copy.deepcopy(existing)),
            atomic_write_json=mock.Mock(side_effect=lambda _path, data: captured.update(data=copy.deepcopy(data))),
            json_file_lock=mock.Mock(side_effect=lambda _path: DummyLock()),
            OUTPUT_MINT=SOL_MINT,
            ACTIVE_TOKEN_CONFIG={"mint": SOL_MINT, "runtime_nonce": "active-generation", "rsi_enabled": True},
            USD_AMOUNT=100.0,
            BUY_ALERTS=[],
            SELL_ALERTS=[],
            ALERT_RESET_MINUTES=0,
            CHECK_INTERVAL=60,
            RSI_CHECK_INTERVAL=5,
            RSI_INTERVAL="1m",
            RSI_RESET_ENABLED=False,
            RSI_ENABLED=True,
            SOLANATRACKER_API_KEY="key",
            SOLANATRACKER_FEATURES_ENABLED=True,
            RSI_STATE={trigger_key: {"triggered": False}},
            last_buy_alert={},
            last_sell_alert={},
            _last_rsi_at=None,
            LATEST_RSI=None,
            LATEST_RSI_TIME=None,
            LATEST_RSI_STATUS="waiting",
            LATEST_RSI_ERROR=None,
            LATEST_RSI_LAST_FETCH_AT=None,
            TOKEN_CHANGED_SINCE_LAST_WRITE=False,
        ), mock.patch.object(self.main.requests, "post", return_value=mock.Mock(ok=True)):
            self.main.check_prices()

        callback.assert_called_once_with(trigger_key, trigger_time)
        self.assertEqual(captured["data"]["last_triggered_rsi"], {trigger_key: trigger_time})
        self.assertEqual(
            captured["data"]["token_states"][SOL_MINT]["last_triggered_rsi"],
            {trigger_key: trigger_time},
        )

    def test_backend_preserves_token_runtime_nonce(self):
        backend = import_backend_module()

        token = backend.normalize_token_entry({"mint": SOL_MINT, "runtime_nonce": "generation-2"})

        self.assertIsNotNone(token)
        self.assertEqual(token["runtime_nonce"], "generation-2")

    def test_backend_rsi_interval_change_validates_and_invalidates_cache(self):
        backend = import_backend_module()
        token = {
            "mint": SOL_MINT,
            "name": "SOL",
            "enabled": True,
            "rsi_enabled": True,
            "rsi_interval": "1m",
        }
        originals = {
            "tokens": backend.state.get("tokens"),
            "active_token_mint": backend.state.get("active_token_mint"),
        }
        cache_update = mock.Mock()
        try:
            backend.state["tokens"] = [token]
            backend.state["active_token_mint"] = None
            payload = backend.TokenUpdatePayload(rsi_interval="5m")
            payload.__fields_set__ = {"rsi_interval"}

            with mock.patch.multiple(
                backend,
                write_config=mock.Mock(),
                update_token_cached_states=cache_update,
                get_token_state_summary=mock.Mock(return_value=[]),
            ):
                result = asyncio.run(backend.update_token(SOL_MINT, payload))

            self.assertTrue(result["success"])
            self.assertEqual(token["rsi_interval"], "5m")
            self.assertTrue(token.get("rsi_refresh_nonce"))
            self.assertEqual(cache_update.call_args.kwargs["rsi_status"], "waiting")

            previous_nonce = token["rsi_refresh_nonce"]
            same_payload = backend.TokenUpdatePayload(rsi_interval="5m")
            same_payload.__fields_set__ = {"rsi_interval"}
            cache_update.reset_mock()
            with mock.patch.multiple(
                backend,
                write_config=mock.Mock(),
                update_token_cached_states=cache_update,
                get_token_state_summary=mock.Mock(return_value=[]),
            ):
                asyncio.run(backend.update_token(SOL_MINT, same_payload))
            self.assertEqual(token["rsi_refresh_nonce"], previous_nonce)
            self.assertIsNone(cache_update.call_args.kwargs["rsi_status"])

            invalid_payload = backend.TokenUpdatePayload(rsi_interval="2m")
            invalid_payload.__fields_set__ = {"rsi_interval"}
            with self.assertRaises(backend.HTTPException) as ctx:
                asyncio.run(backend.update_token(SOL_MINT, invalid_payload))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(token["rsi_interval"], "5m")
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]

    def test_backend_readd_clears_stale_generation_state(self):
        backend = import_backend_module()
        mint = "TokenReaddedBeforeOldWriteFinished"
        existing = {
            "token_states": {
                mint: {
                    "runtime_nonce": "old-generation",
                    "last_triggered_buy": {"1.00000000": "2026-07-28T12:00:00+00:00"},
                    "last_triggered_rsi": {"above:70.00": "2026-07-28T12:00:00+00:00"},
                }
            },
            "token_price_history": {mint: [{"timestamp": "2026-07-28T12:00:00+00:00", "buy_price": 1.0}]},
        }
        captured = {}

        class DummyLock:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch.multiple(
            backend,
            read_json_file=mock.Mock(return_value=copy.deepcopy(existing)),
            atomic_write_json=mock.Mock(side_effect=lambda _path, data: captured.update(data=copy.deepcopy(data))),
            json_file_lock=mock.Mock(side_effect=lambda _path: DummyLock()),
        ):
            backend.update_token_cached_states(
                {"mint": mint, "runtime_nonce": "new-generation"},
                reset_runtime=True,
            )

        token_state = captured["data"]["token_states"][mint]
        self.assertEqual(token_state, {"runtime_nonce": "new-generation"})
        self.assertNotIn(mint, captured["data"]["token_price_history"])

    def test_backend_summary_exposes_price_verification_due_time(self):
        backend = import_backend_module()
        due_at = "2026-07-29T12:00:05+00:00"
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "solanatracker_features_enabled": backend.state.get("solanatracker_features_enabled"),
            "read_json_file": backend.read_json_file,
        }
        try:
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["solanatracker_features_enabled"] = False
            backend.read_json_file = lambda _path: {
                "token_states": {
                    SOL_MINT: {
                        "last_checked_at": "2026-07-29T11:59:00+00:00",
                        "price_status": "verifying",
                        "price_message": "Verifying an unusual Jupiter price move",
                        "price_verification_due_at": due_at,
                    }
                }
            }

            summary = backend.get_token_state_summary()[0]

            self.assertEqual(summary["price_status"], "verifying")
            self.assertEqual(summary["price_verification_due_at"], due_at)
            self.assertEqual(summary["next_check_at"], due_at)
            self.assertIsNone(summary["error"])
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["solanatracker_features_enabled"] = originals["solanatracker_features_enabled"]
            backend.read_json_file = originals["read_json_file"]
    def test_backend_summary_derives_next_from_actual_check_and_current_interval(self):
        backend = import_backend_module()
        checked_at = "2026-07-28T12:00:00+00:00"
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "check_interval": backend.state.get("check_interval"),
            "read_json_file": backend.read_json_file,
        }
        try:
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True, "check_interval": 120}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["check_interval"] = 60
            backend.read_json_file = lambda path: {
                "token_states": {
                    SOL_MINT: {
                        "timestamp": "2026-07-28T12:05:00+00:00",
                        "last_checked_at": checked_at,
                        "next_check_at": "2026-07-28T11:00:00+00:00",
                    }
                }
            }

            summary = backend.get_token_state_summary()[0]
            expected_next = (backend.parse_iso_to_utc(checked_at) + timedelta(seconds=120)).isoformat()

            self.assertEqual(summary["last_checked"], checked_at)
            self.assertEqual(summary["next_check_at"], expected_next)
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["check_interval"] = originals["check_interval"]
            backend.read_json_file = originals["read_json_file"]

    def test_backend_exposes_legacy_rsi_as_stale_until_verified(self):
        backend = import_backend_module()
        original_env = os.environ.get("SOLANATRACKER_API_KEY")
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "solanatracker_features_enabled": backend.state.get("solanatracker_features_enabled"),
            "rsi_enabled": backend.state.get("rsi_enabled"),
            "read_json_file": backend.read_json_file,
        }
        token_state = {
            "latest_rsi": 35.0,
            "latest_rsi_time": "2026-07-30T12:00:00+00:00",
            "rsi_status": "ok",
            "rsi_error": None,
        }
        cached = {**token_state, "token_states": {SOL_MINT: token_state}}
        try:
            os.environ["SOLANATRACKER_API_KEY"] = "key"
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True, "rsi_enabled": True}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["solanatracker_features_enabled"] = True
            backend.state["rsi_enabled"] = True
            backend.read_json_file = lambda _path: copy.deepcopy(cached)

            summary = backend.get_token_state_summary()[0]
            status = asyncio.run(backend.get_rsi_status())

            self.assertEqual(summary["rsi"], 35.0)
            self.assertEqual(summary["rsi_status"], "stale")
            self.assertTrue(summary["rsi_migration_pending"])
            self.assertIn("earlier app version", summary["error"])
            self.assertEqual(status["latest_rsi"], 35.0)
            self.assertEqual(status["status"], "stale")
            self.assertTrue(status["migration_pending"])
            self.assertEqual(status["value_calculation_version"], "")
        finally:
            if original_env is None:
                os.environ.pop("SOLANATRACKER_API_KEY", None)
            else:
                os.environ["SOLANATRACKER_API_KEY"] = original_env
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["solanatracker_features_enabled"] = originals["solanatracker_features_enabled"]
            backend.state["rsi_enabled"] = originals["rsi_enabled"]
            backend.read_json_file = originals["read_json_file"]

    def test_backend_hides_old_rsi_while_new_trade_history_is_pending(self):
        backend = import_backend_module()
        original_env = os.environ.get("SOLANATRACKER_API_KEY")
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "solanatracker_features_enabled": backend.state.get("solanatracker_features_enabled"),
            "rsi_enabled": backend.state.get("rsi_enabled"),
            "read_json_file": backend.read_json_file,
        }
        pending_source = {
            "timestamp": "2026-07-24T12:00:00+00:00",
            "close": 50.0,
            "volume": 2.0,
            "interval": "1m",
            "active_bars": 1,
            "calculation_version": backend.RSI_CALCULATION_VERSION,
        }
        token_state = {
            "latest_rsi": 49.0,
            "latest_rsi_time": "2026-07-20T12:00:00+00:00",
            "rsi_status": "waiting",
            "rsi_error": "New traded RSI candle detected; verifying historical candles",
            "rsi_pending_source": pending_source,
            "rsi_backfill_failures": 2,
            "rsi_backfill_next_attempt_at": "2026-07-31T18:00:00+00:00",
            "rsi_value_calculation_version": backend.RSI_CALCULATION_VERSION,
        }
        cached = {**token_state, "token_states": {SOL_MINT: token_state}}
        try:
            os.environ["SOLANATRACKER_API_KEY"] = "key"
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True, "rsi_enabled": True}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["solanatracker_features_enabled"] = True
            backend.state["rsi_enabled"] = True
            backend.read_json_file = lambda _path: copy.deepcopy(cached)

            summary = backend.get_token_state_summary()[0]
            state_payload = asyncio.run(backend.get_state())
            status = asyncio.run(backend.get_rsi_status())

            self.assertIsNone(summary["rsi"])
            self.assertEqual(summary["rsi_status"], "waiting")
            self.assertTrue(summary["rsi_refresh_pending"])
            self.assertEqual(summary["rsi_backfill_failures"], 2)
            self.assertEqual(summary["rsi_backfill_next_attempt_at"], "2026-07-31T18:00:00+00:00")
            self.assertIsNone(summary["error"])
            self.assertIsNone(state_payload["latest_rsi"])
            self.assertEqual(state_payload["rsi_status"], "waiting")
            self.assertTrue(state_payload["rsi_refresh_pending"])
            self.assertEqual(state_payload["rsi_backfill_failures"], 2)
            self.assertEqual(state_payload["rsi_backfill_next_attempt_at"], "2026-07-31T18:00:00+00:00")
            self.assertIsNone(status["latest_rsi"])
            self.assertEqual(status["status"], "waiting")
            self.assertTrue(status["refresh_pending"])
            self.assertEqual(status["backfill_failures"], 2)
            self.assertEqual(status["backfill_next_attempt_at"], "2026-07-31T18:00:00+00:00")
            self.assertEqual(status["pending_source"]["timestamp"], pending_source["timestamp"])
            self.assertIn("verifying", status["message"])
        finally:
            if original_env is None:
                os.environ.pop("SOLANATRACKER_API_KEY", None)
            else:
                os.environ["SOLANATRACKER_API_KEY"] = original_env
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["solanatracker_features_enabled"] = originals["solanatracker_features_enabled"]
            backend.state["rsi_enabled"] = originals["rsi_enabled"]
            backend.read_json_file = originals["read_json_file"]
    def test_backend_token_summary_suppresses_rsi_when_solanatracker_disabled(self):
        backend = import_backend_module()
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "solanatracker_features_enabled": backend.state.get("solanatracker_features_enabled"),
            "read_json_file": backend.read_json_file,
        }
        try:
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True, "rsi_enabled": True}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["solanatracker_features_enabled"] = False
            backend.read_json_file = lambda path: {"token_states": {SOL_MINT: {"latest_rsi": 55.5, "rsi_status": "ok"}}}

            summary = backend.get_token_state_summary()[0]
            self.assertIsNone(summary["rsi"])
            self.assertEqual(summary["rsi_status"], "disabled")
            self.assertIsNone(summary["error"])
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["solanatracker_features_enabled"] = originals["solanatracker_features_enabled"]
            backend.read_json_file = originals["read_json_file"]

    def test_backend_token_summary_suppresses_rsi_without_api_key(self):
        backend = import_backend_module()
        original_env = os.environ.get("SOLANATRACKER_API_KEY")
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "solanatracker_features_enabled": backend.state.get("solanatracker_features_enabled"),
            "read_json_file": backend.read_json_file,
        }
        try:
            os.environ.pop("SOLANATRACKER_API_KEY", None)
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True, "rsi_enabled": True}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["solanatracker_features_enabled"] = True
            backend.read_json_file = lambda path: {"token_states": {SOL_MINT: {"latest_rsi": 55.5, "rsi_status": "ok"}}}

            summary = backend.get_token_state_summary()[0]
            self.assertIsNone(summary["rsi"])
            self.assertEqual(summary["rsi_status"], "disabled")
            self.assertTrue(summary["rsi_enabled"])
            self.assertIsNone(summary["error"])

            status = asyncio.run(backend.get_rsi_status())
            self.assertIsNone(status["latest_rsi"])
            self.assertEqual(status["status"], "disabled")
            self.assertFalse(status["solanatracker_api_key_configured"])
        finally:
            if original_env is None:
                os.environ.pop("SOLANATRACKER_API_KEY", None)
            else:
                os.environ["SOLANATRACKER_API_KEY"] = original_env
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["solanatracker_features_enabled"] = originals["solanatracker_features_enabled"]
            backend.read_json_file = originals["read_json_file"]
    def test_backend_token_summary_suppresses_per_token_rsi_disabled(self):
        backend = import_backend_module()
        original_env = os.environ.get("SOLANATRACKER_API_KEY")
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "solanatracker_features_enabled": backend.state.get("solanatracker_features_enabled"),
            "read_json_file": backend.read_json_file,
        }
        try:
            os.environ["SOLANATRACKER_API_KEY"] = "key"
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL", "enabled": True, "rsi_enabled": False}]
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["solanatracker_features_enabled"] = True
            backend.read_json_file = lambda path: {"token_states": {SOL_MINT: {"latest_rsi": 55.5, "rsi_status": "ok"}}}

            summary = backend.get_token_state_summary()[0]
            self.assertIsNone(summary["rsi"])
            self.assertEqual(summary["rsi_status"], "disabled")
            self.assertFalse(summary["rsi_enabled"])
            self.assertIsNone(summary["error"])
        finally:
            if original_env is None:
                os.environ.pop("SOLANATRACKER_API_KEY", None)
            else:
                os.environ["SOLANATRACKER_API_KEY"] = original_env
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["solanatracker_features_enabled"] = originals["solanatracker_features_enabled"]
            backend.read_json_file = originals["read_json_file"]
    def test_active_token_rsi_triggers_survive_first_write_after_switch(self):
        trigger_key = "above:70.00"
        trigger_time = "2026-06-24T12:00:00+00:00"
        existing = {
            "token_states": {
                SOL_MINT: {"last_triggered_rsi": {trigger_key: trigger_time}}
            },
            "last_triggered_rsi": {},
        }
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
            "active_token_config": self.main.ACTIVE_TOKEN_CONFIG,
            "token_changed": self.main.TOKEN_CHANGED_SINCE_LAST_WRITE,
        }
        try:
            self.main.OUTPUT_MINT = SOL_MINT
            self.main.ACTIVE_TOKEN_CONFIG = {"mint": SOL_MINT, "name": "SOL", "ntfy_topic": ""}
            self.main.TOKEN_CHANGED_SINCE_LAST_WRITE = True
            self.main.last_buy_alert.clear()
            self.main.last_sell_alert.clear()
            self.main.read_json_file = lambda path: copy.deepcopy(existing)
            self.main.atomic_write_json = lambda path, data: captured.update(copy.deepcopy(data))
            self.main.json_file_lock = lambda path: DummyLock()
            self.main.resolve_token_decimals = lambda mint, configured_decimals=None: 6

            self.main.write_status_json(None, None, None, None)

            self.assertEqual(captured["token_states"][SOL_MINT]["last_triggered_rsi"], {trigger_key: trigger_time})
            self.assertEqual(captured["last_triggered_rsi"], {trigger_key: trigger_time})
            self.assertFalse(self.main.TOKEN_CHANGED_SINCE_LAST_WRITE)
        finally:
            self.main.read_json_file = originals["read_json_file"]
            self.main.atomic_write_json = originals["atomic_write_json"]
            self.main.json_file_lock = originals["json_file_lock"]
            self.main.resolve_token_decimals = originals["resolve_token_decimals"]
            self.main.OUTPUT_MINT = originals["output_mint"]
            self.main.ACTIVE_TOKEN_CONFIG = originals["active_token_config"]
            self.main.TOKEN_CHANGED_SINCE_LAST_WRITE = originals["token_changed"]

    def test_active_status_schedule_only_advances_on_completed_checks(self):
        old_checked = "2026-07-28T12:00:00+00:00"
        old_next = "2026-07-28T12:01:00+00:00"
        store = {
            "data": {
                "token_states": {
                    SOL_MINT: {
                        "timestamp": old_checked,
                        "last_checked_at": old_checked,
                        "next_check_at": old_next,
                        "buy_price": 0.9,
                        "sell_price": 0.8,
                        "error": "old price failure",
                    }
                },
                "scheduler": {"last_checked_mint": SOL_MINT, "last_checked_at": old_checked},
            }
        }

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
            "active_token_config": self.main.ACTIVE_TOKEN_CONFIG,
            "check_interval": self.main.CHECK_INTERVAL,
            "last_buy": dict(self.main.last_buy_alert),
            "last_sell": dict(self.main.last_sell_alert),
        }
        try:
            self.main.OUTPUT_MINT = SOL_MINT
            self.main.ACTIVE_TOKEN_CONFIG = {"mint": SOL_MINT, "name": "SOL", "ntfy_topic": ""}
            self.main.CHECK_INTERVAL = 60
            self.main.last_buy_alert.clear()
            self.main.last_sell_alert.clear()
            self.main.read_json_file = lambda path: copy.deepcopy(store["data"])
            self.main.atomic_write_json = lambda path, data: store.update(data=copy.deepcopy(data))
            self.main.json_file_lock = lambda path: DummyLock()
            self.main.resolve_token_decimals = lambda mint, configured_decimals=None: 6

            self.main.write_status_json(1.25, 1.2, 80, 96, record_history=False, error=None)
            completed = copy.deepcopy(store["data"])
            token_state = completed["token_states"][SOL_MINT]
            checked = self.main.parse_iso_to_utc(token_state["last_checked_at"])
            next_check = self.main.parse_iso_to_utc(token_state["next_check_at"])

            self.assertAlmostEqual((next_check - checked).total_seconds(), 60, delta=1)
            self.assertIsNone(token_state["error"])
            self.assertEqual(token_state["buy_price"], 1.25)

            self.main.write_status_json(None, None, None, None, check_completed=False)
            non_check = store["data"]
            non_check_state = non_check["token_states"][SOL_MINT]

            for field in ("timestamp", "last_checked_at", "next_check_at", "buy_price", "sell_price", "error"):
                self.assertEqual(non_check_state[field], token_state[field])
            self.assertEqual(non_check["scheduler"], completed["scheduler"])
        finally:
            self.main.read_json_file = originals["read_json_file"]
            self.main.atomic_write_json = originals["atomic_write_json"]
            self.main.json_file_lock = originals["json_file_lock"]
            self.main.resolve_token_decimals = originals["resolve_token_decimals"]
            self.main.OUTPUT_MINT = originals["output_mint"]
            self.main.ACTIVE_TOKEN_CONFIG = originals["active_token_config"]
            self.main.CHECK_INTERVAL = originals["check_interval"]
            self.main.last_buy_alert.clear()
            self.main.last_buy_alert.update(originals["last_buy"])
            self.main.last_sell_alert.clear()
            self.main.last_sell_alert.update(originals["last_sell"])

    def test_backend_trigger_reset_syncs_active_token_cache(self):
        backend = import_backend_module()
        trigger_key = "above:70.00"
        buy_key = "0.12345678"
        existing = {
            "token_states": {
                SOL_MINT: {
                    "last_triggered_rsi": {trigger_key: "2026-06-24T12:00:00+00:00"},
                    "last_triggered_buy": {buy_key: "2026-06-24T12:00:00+00:00"},
                    "latest_rsi": 72.5,
                },
                "OtherToken": {
                    "last_triggered_rsi": {trigger_key: "keep"},
                    "last_triggered_buy": {buy_key: "keep"},
                },
            },
            "last_triggered_rsi": {trigger_key: "2026-06-24T12:00:00+00:00"},
            "last_triggered_buy": {buy_key: "2026-06-24T12:00:00+00:00"},
        }
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
            "active_token_mint": backend.state.get("active_token_mint"),
            "last_triggered_rsi": dict(backend.state.get("last_triggered_rsi", {})),
            "last_triggered_buy": dict(backend.state.get("last_triggered_buy", {})),
        }
        try:
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["last_triggered_rsi"] = {}
            backend.state["last_triggered_buy"] = {}
            backend.read_json_file = lambda path: copy.deepcopy(existing)
            backend.atomic_write_json = lambda path, data: captured.clear() or captured.update(copy.deepcopy(data))
            backend.json_file_lock = lambda path: DummyLock()

            backend.update_active_token_rsi_trigger(trigger_key, remove=True)

            self.assertEqual(captured["last_triggered_rsi"], {})
            self.assertEqual(captured["token_states"][SOL_MINT]["last_triggered_rsi"], {})
            self.assertEqual(captured["token_states"][SOL_MINT]["latest_rsi"], 72.5)
            self.assertEqual(captured["token_states"]["OtherToken"]["last_triggered_rsi"], {trigger_key: "keep"})

            backend.update_active_token_trigger_cache("buy", buy_key, remove=True)

            self.assertEqual(captured["last_triggered_buy"], {})
            self.assertEqual(captured["token_states"][SOL_MINT]["last_triggered_buy"], {})
            self.assertEqual(captured["token_states"]["OtherToken"]["last_triggered_buy"], {buy_key: "keep"})
        finally:
            backend.read_json_file = originals["read_json_file"]
            backend.atomic_write_json = originals["atomic_write_json"]
            backend.json_file_lock = originals["json_file_lock"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["last_triggered_rsi"] = originals["last_triggered_rsi"]
            backend.state["last_triggered_buy"] = originals["last_triggered_buy"]

    def test_backend_active_token_switch_restores_cached_state(self):
        backend = import_backend_module()
        trigger_key = "above:70.00"
        buy_key = "0.12345678"
        history_point = {"timestamp": datetime.now(timezone.utc).isoformat(), "buy_price": 1.23, "sell_price": 1.24}
        existing = {
            "token_price_history": {SOL_MINT: [history_point]},
            "token_states": {
                SOL_MINT: {
                    "buy_price": 1.23,
                    "sell_price": 1.24,
                    "token_received": 81.3,
                    "usdc_returned": 99.5,
                    "latest_rsi": 64.25,
                    "latest_rsi_time": "2026-06-24T12:00:00+00:00",
                    "rsi_status": "ok",
                    "rsi_error": None,
                    "rsi_last_fetch_at": "2026-06-24T12:00:01+00:00",
                    "last_triggered_buy": {buy_key: "2026-06-24T12:00:00+00:00"},
                    "rsi_last_attempt_at": "2026-06-24T12:00:02+00:00",
                    "rsi_last_success_at": "2026-06-24T12:00:01+00:00",
                    "rsi_source": {
                        "timestamp": "2026-06-24T12:00:00+00:00", "close": 1.2,
                        "volume": 5.0, "interval": "1m",
                    },
                    "rsi_backfill_failures": 3,
                    "rsi_backfill_next_attempt_at": "2026-07-31T18:00:00+00:00",
                    "last_triggered_sell": {},
                    "last_triggered_rsi": {trigger_key: "2026-06-24T12:00:00+00:00"},
                }
            },
        }
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
            "active_token_mint": backend.state.get("active_token_mint"),
            "latest_prices": list(backend.state.get("latest_prices", [])),
            "last_triggered_buy": dict(backend.state.get("last_triggered_buy", {})),
            "last_triggered_sell": dict(backend.state.get("last_triggered_sell", {})),
            "last_triggered_rsi": dict(backend.state.get("last_triggered_rsi", {})),
        }
        try:
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["latest_prices"] = []
            backend.state["last_triggered_buy"] = {}
            backend.state["last_triggered_sell"] = {}
            backend.state["last_triggered_rsi"] = {}
            backend.read_json_file = lambda path: copy.deepcopy(existing)
            backend.atomic_write_json = lambda path, data: captured.update(copy.deepcopy(data))
            backend.json_file_lock = lambda path: DummyLock()

            backend.clear_active_runtime_cache()

            self.assertEqual(backend.state["latest_prices"], [history_point])
            self.assertEqual(backend.state["last_triggered_buy"], {buy_key: "2026-06-24T12:00:00+00:00"})
            self.assertEqual(backend.state["last_triggered_rsi"], {trigger_key: "2026-06-24T12:00:00+00:00"})
            self.assertEqual(captured["latest_prices"], [history_point])
            self.assertEqual(captured["latest_rsi"], 64.25)
            self.assertEqual(captured["rsi_last_attempt_at"], "2026-06-24T12:00:02+00:00")
            self.assertEqual(captured["rsi_last_success_at"], "2026-06-24T12:00:01+00:00")
            self.assertEqual(captured["rsi_source"]["timestamp"], "2026-06-24T12:00:00+00:00")
            self.assertEqual(captured["rsi_source"]["interval"], "1m")
            self.assertEqual(captured["rsi_backfill_failures"], 3)
            self.assertEqual(captured["rsi_backfill_next_attempt_at"], "2026-07-31T18:00:00+00:00")
            self.assertEqual(captured["last_triggered_buy"], {buy_key: "2026-06-24T12:00:00+00:00"})
            self.assertEqual(captured["last_triggered_rsi"], {trigger_key: "2026-06-24T12:00:00+00:00"})
        finally:
            backend.read_json_file = originals["read_json_file"]
            backend.atomic_write_json = originals["atomic_write_json"]
            backend.json_file_lock = originals["json_file_lock"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["latest_prices"] = originals["latest_prices"]
            backend.state["last_triggered_buy"] = originals["last_triggered_buy"]
            backend.state["last_triggered_sell"] = originals["last_triggered_sell"]
            backend.state["last_triggered_rsi"] = originals["last_triggered_rsi"]

    def test_backend_add_token_rejects_duplicate_before_jupiter_validation(self):
        backend = import_backend_module()
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "validate_token": backend.validate_token,
            "write_config": backend.write_config,
        }
        calls = {"validate": 0}

        async def fail_if_called(payload):
            calls["validate"] += 1
            raise AssertionError("Jupiter validation should not run for duplicate tokens")

        try:
            backend.state["tokens"] = [{"mint": SOL_MINT, "name": "SOL"}]
            backend.validate_token = fail_if_called
            backend.write_config = lambda: None
            payload = backend.TokenPayload(
                mint=SOL_MINT,
                name=None,
                enabled=True,
                ntfy_topic=None,
                check_interval=None,
                rsi_check_interval=None,
                rsi_interval=None,
                rsi_reset_enabled=None,
            )

            with self.assertRaises(backend.HTTPException) as ctx:
                asyncio.run(backend.add_token(payload))

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(calls["validate"], 0)
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.validate_token = originals["validate_token"]
            backend.write_config = originals["write_config"]

    def test_backend_clear_active_price_history_is_token_scoped(self):
        backend = import_backend_module()
        other_mint = USDC_MINT
        active_point = {"timestamp": "2026-06-24T12:00:00+00:00", "buy_price": 1.23, "sell_price": 1.24}
        other_point = {"timestamp": "2026-06-24T12:01:00+00:00", "buy_price": 2.0, "sell_price": 2.1}
        existing = {
            "latest_prices": [active_point],
            "price_per_token_buy": 1.23,
            "price_per_token_sell": 1.24,
            "token_received": 81.3,
            "usdc_returned": 99.5,
            "token_price_history": {SOL_MINT: [active_point], other_mint: [other_point]},
            "token_states": {
                SOL_MINT: {"buy_price": 1.23, "sell_price": 1.24, "token_received": 81.3, "usdc_returned": 99.5},
                other_mint: {"buy_price": 2.0, "sell_price": 2.1},
            },
        }
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
            "active_token_mint": backend.state.get("active_token_mint"),
            "latest_prices": list(backend.state.get("latest_prices", [])),
            "token_price_history": copy.deepcopy(backend.state.get("token_price_history", {})),
        }
        try:
            backend.state["active_token_mint"] = SOL_MINT
            backend.state["latest_prices"] = [active_point]
            backend.state["token_price_history"] = {SOL_MINT: [active_point], other_mint: [other_point]}
            backend.read_json_file = lambda path: copy.deepcopy(existing)
            backend.atomic_write_json = lambda path, data: captured.update(copy.deepcopy(data))
            backend.json_file_lock = lambda path: DummyLock()

            backend.clear_active_price_history()

            self.assertEqual(backend.state["latest_prices"], [])
            self.assertNotIn(SOL_MINT, backend.state["token_price_history"])
            self.assertEqual(backend.state["token_price_history"][other_mint], [other_point])
            self.assertEqual(captured["latest_prices"], [])
            self.assertNotIn(SOL_MINT, captured["token_price_history"])
            self.assertEqual(captured["token_price_history"][other_mint], [other_point])
            self.assertIsNone(captured["token_states"][SOL_MINT]["buy_price"])
            self.assertEqual(captured["token_states"][other_mint]["buy_price"], 2.0)
        finally:
            backend.read_json_file = originals["read_json_file"]
            backend.atomic_write_json = originals["atomic_write_json"]
            backend.json_file_lock = originals["json_file_lock"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["latest_prices"] = originals["latest_prices"]
            backend.state["token_price_history"] = originals["token_price_history"]

    def test_token_price_history_is_bounded_and_per_token(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        current = datetime.now(timezone.utc).isoformat()
        older_recent = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        newer_recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        existing = {
            "token_price_history": {
                "TokenA": [
                    {"timestamp": old, "buy_price": 1, "sell_price": 2},
                    {"timestamp": recent, "buy_price": 3, "sell_price": 4},
                ]
            }
        }

        points = self.main.append_token_price_history(existing, "TokenA", current, 5, 6)

        self.assertEqual(len(points), 2)
        self.assertEqual(points[-1]["buy_price"], 5)

        self.main.append_token_price_history(existing, "TokenA", current, 7, 8)

        self.assertEqual(len(existing["token_price_history"]["TokenA"]), 2)
        self.assertEqual(existing["token_price_history"]["TokenA"][-1]["buy_price"], 7)

        self.main.append_token_price_history(existing, "TokenB", current, 9, None)

        self.assertIn("TokenB", existing["token_price_history"])
        self.assertIsNone(existing["token_price_history"]["TokenB"][-1]["sell_price"])

        sorted_points = self.main.prune_history_points([
            {"timestamp": newer_recent, "buy_price": 2},
            {"timestamp": older_recent, "buy_price": 1},
        ])

        self.assertEqual([point["timestamp"] for point in sorted_points], sorted(point["timestamp"] for point in sorted_points))

    def test_backend_wallets_are_active_token_scoped(self):
        backend = import_backend_module()
        other_mint = USDC_MINT
        originals = {
            "tokens": copy.deepcopy(backend.state.get("tokens", [])),
            "active_token_mint": backend.state.get("active_token_mint"),
            "wallet_addresses": list(backend.state.get("wallet_addresses", [])),
            "write_config": backend.write_config,
            "prune_pnl_cache": backend.prune_pnl_cache,
        }
        pruned = []
        try:
            active_token = backend.normalize_token_entry({"mint": SOL_MINT, "name": "SOL", "wallet_addresses": ["WalletA"]})
            other_token = backend.normalize_token_entry({"mint": other_mint, "name": "OTHER", "wallet_addresses": ["WalletB"]})
            backend.state["tokens"] = [active_token, other_token]
            backend.state["active_token_mint"] = SOL_MINT
            backend.write_config = lambda: None
            backend.prune_pnl_cache = lambda mint: pruned.append(mint)
            backend.apply_active_token_to_legacy()

            asyncio.run(backend.add_wallets(backend.AddressesList(values=["WalletC", "WalletA"])))

            self.assertEqual(backend.state["wallet_addresses"], ["WalletA", "WalletC"])
            self.assertEqual(backend.state["tokens"][0]["wallet_addresses"], ["WalletA", "WalletC"])
            self.assertEqual(backend.state["tokens"][1]["wallet_addresses"], ["WalletB"])
            self.assertEqual(pruned[-1], SOL_MINT)

            asyncio.run(backend.delete_wallet(backend.AddressValue(value="WalletA")))

            self.assertEqual(backend.state["wallet_addresses"], ["WalletC"])
            self.assertEqual(backend.state["tokens"][0]["wallet_addresses"], ["WalletC"])
            self.assertEqual(backend.state["tokens"][1]["wallet_addresses"], ["WalletB"])
        finally:
            backend.state["tokens"] = originals["tokens"]
            backend.state["active_token_mint"] = originals["active_token_mint"]
            backend.state["wallet_addresses"] = originals["wallet_addresses"]
            backend.write_config = originals["write_config"]
            backend.prune_pnl_cache = originals["prune_pnl_cache"]

    def test_main_global_ntfy_topic_resolver_does_not_keep_stale_topic(self):
        original_topic = self.main.NTFY_TOPIC
        original_env = os.environ.get("NTFY_TOPIC")
        try:
            self.main.NTFY_TOPIC = "OldTopic"
            os.environ.pop("NTFY_TOPIC", None)
            self.assertEqual(self.main.resolve_global_ntfy_topic({"ntfy_topic": ""}), "")
            self.assertEqual(self.main.resolve_global_ntfy_topic({}), "")

            os.environ["NTFY_TOPIC"] = "EnvTopic"
            self.assertEqual(self.main.resolve_global_ntfy_topic({"ntfy_topic": ""}), "EnvTopic")
            self.assertEqual(self.main.resolve_global_ntfy_topic({}), "EnvTopic")
            self.assertEqual(self.main.resolve_global_ntfy_topic({"ntfy_topic": "AppTopic"}), "AppTopic")
        finally:
            self.main.NTFY_TOPIC = original_topic
            if original_env is None:
                os.environ.pop("NTFY_TOPIC", None)
            else:
                os.environ["NTFY_TOPIC"] = original_env

    def test_main_token_ntfy_topic_uses_token_before_global(self):
        original_topic = self.main.NTFY_TOPIC
        try:
            self.main.NTFY_TOPIC = "GlobalTopic"
            self.assertEqual(self.main.token_ntfy_topic({"ntfy_topic": ""}), ("GlobalTopic", "inherited"))
            self.assertEqual(self.main.token_ntfy_topic({"ntfy_topic": "TokenTopic"}), ("TokenTopic", "custom"))

            self.main.NTFY_TOPIC = ""
            self.assertEqual(self.main.token_ntfy_topic({"ntfy_topic": ""}), ("", "disabled"))
        finally:
            self.main.NTFY_TOPIC = original_topic

    def test_backend_global_ntfy_topic_overrides_env_and_tokens_can_inherit(self):
        backend = import_backend_module()
        original_topic = backend.state.get("ntfy_topic")
        original_env = os.environ.get("NTFY_TOPIC")
        inherited_token = {"ntfy_topic": ""}
        custom_token = {"ntfy_topic": "TokenTopic"}
        try:
            os.environ["NTFY_TOPIC"] = "EnvTopic"
            backend.state["ntfy_topic"] = ""
            self.assertEqual(backend.effective_ntfy_topic(inherited_token), "EnvTopic")
            self.assertEqual(backend.ntfy_topic_source(inherited_token), "inherited")

            backend.state["ntfy_topic"] = "AppTopic"
            self.assertEqual(backend.effective_ntfy_topic(inherited_token), "AppTopic")
            self.assertEqual(backend.ntfy_topic_source(inherited_token), "inherited")
            self.assertEqual(backend.effective_ntfy_topic(custom_token), "TokenTopic")
            self.assertEqual(backend.ntfy_topic_source(custom_token), "custom")

            backend.state["ntfy_topic"] = ""
            os.environ.pop("NTFY_TOPIC", None)
            self.assertEqual(backend.effective_ntfy_topic(inherited_token), "")
            self.assertEqual(backend.ntfy_topic_source(inherited_token), "disabled")
        finally:
            backend.state["ntfy_topic"] = original_topic
            if original_env is None:
                os.environ.pop("NTFY_TOPIC", None)
            else:
                os.environ["NTFY_TOPIC"] = original_env

    def test_backend_settings_rejects_invalid_global_ntfy_topic(self):
        backend = import_backend_module()
        original_topic = backend.state.get("ntfy_topic")
        original_write_config = backend.write_config
        try:
            backend.write_config = lambda: None
            settings = backend.RuntimeSettings(ntfy_topic="bad/topic")
            settings.__fields_set__ = {"ntfy_topic"}
            with self.assertRaises(backend.HTTPException) as ctx:
                asyncio.run(backend.update_settings(settings))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(backend.state.get("ntfy_topic"), original_topic)
        finally:
            backend.state["ntfy_topic"] = original_topic
            backend.write_config = original_write_config
    def test_backend_rsi_warmup_messages_include_no_candle_cases(self):
        backend = import_backend_module()
        warmup_messages = [
            "Not enough bars for RSI(14): got 2",
            "Not enough traded bars for RSI(14): 9/15 available",
            "Not enough data for RSI: need >= 15 points, got 2",
            "No RSI candles returned in the last 3 days",
            "No valid RSI candles returned in the last 3 days",
            "No non-zero volume bars in the last 3 days",
        ]
        for message in warmup_messages:
            with self.subTest(message=message):
                self.assertTrue(backend.is_rsi_warmup_message(message))

        self.assertFalse(backend.is_rsi_warmup_message("401 Unauthorized"))

    def test_backend_wallet_list_normalization_deduplicates(self):
        backend = import_backend_module()
        self.assertEqual(
            backend.normalize_wallet_addresses([" WalletA ", "WalletA", "", None, "WalletB"]),
            ["WalletA", "WalletB"],
        )

    def test_backend_extracts_solanatracker_pnl_v2_position(self):
        backend = import_backend_module()
        result = backend.extract_pnl_result({
            "wallet": "WalletA",
            "current": {"balance": 10, "value": 25, "avgCost": 1.2, "costBasis": 12},
            "pnl": {"token": {"realized": 3, "unrealized": 13, "total": 16}},
            "timing": {"lastTrade": 1_710_000_000_000},
        }, attempts=1)

        self.assertEqual(result["pnl_status"], "ok")
        self.assertEqual(result["holding"], 10)
        self.assertEqual(result["current_value"], 25)
        self.assertEqual(result["cost_basis"], 1.2)
        self.assertEqual(result["cost_basis_total"], 12)
        self.assertEqual(result["realized"], 3)
        self.assertEqual(result["unrealized"], 13)
        self.assertIn("+00:00", result["last_trade_time"])

    def test_backend_accepts_all_documented_pnl_modes(self):
        backend = import_backend_module()
        original_mode = backend.SOLANATRACKER_PNL_MODE
        try:
            for mode in ["strict", "adjusted", "raw"]:
                backend.SOLANATRACKER_PNL_MODE = mode
                self.assertEqual(backend._solanatracker_pnl_mode(), mode)
            backend.SOLANATRACKER_PNL_MODE = "bad"
            self.assertEqual(backend._solanatracker_pnl_mode(), "adjusted")
        finally:
            backend.SOLANATRACKER_PNL_MODE = original_mode
    def test_backend_pnl_batch_retries_raw_wallet_array_body(self):
        backend = import_backend_module()
        original_request = backend._request_solanatracker_json
        calls = []
        try:
            def fake_request(method, url, headers, *, params=None, json_body=None, timeout=10):
                calls.append(json_body)
                if isinstance(json_body, dict):
                    raise backend.SolanaTrackerRequestError("Body shape rejected", status_code=422)
                return {
                    "positions": [{
                        "wallet": "WalletA",
                        "current": {"balance": 3, "value": 9, "avgCost": 2, "costBasis": 6},
                        "pnl": {"token": {"realized": 1, "unrealized": 3, "total": 4}},
                    }]
                }, 1

            backend._request_solanatracker_json = fake_request
            result = backend.fetch_pnl_batch_results(["WalletA"], SOL_MINT, "key")

            self.assertEqual(calls[0], {"wallets": ["WalletA"]})
            self.assertEqual(calls[1], ["WalletA"])
            self.assertEqual(result["WalletA"]["pnl_status"], "ok")
            self.assertEqual(result["WalletA"]["holding"], 3)
            self.assertEqual(result["WalletA"]["cost_basis"], 2)
        finally:
            backend._request_solanatracker_json = original_request
    def test_backend_pnl_batch_falls_back_to_basic_wallet_holding(self):
        backend = import_backend_module()
        original_request = backend._request_solanatracker_json
        original_throttle = backend.throttle
        calls = []
        try:
            backend.throttle = lambda: None

            def fake_request(method, url, headers, *, params=None, json_body=None, timeout=10):
                calls.append((method, url, json_body))
                if method == "POST":
                    return {
                        "positions": [{
                            "wallet": "WalletA",
                            "current": {"balance": 1, "value": 4, "avgCost": 2, "costBasis": 2},
                            "pnl": {"token": {"realized": 0, "unrealized": 2, "total": 2}},
                        }],
                        "notFound": ["WalletB"],
                    }, 1
                return {"tokens": [{"address": SOL_MINT, "balance": 2, "value": 8, "price": {"usd": 4}}]}, 1

            backend._request_solanatracker_json = fake_request
            result = backend.fetch_pnl_batch_results(["WalletA", "WalletB"], SOL_MINT, "key")

            self.assertEqual(result["WalletA"]["pnl_status"], "ok")
            self.assertEqual(result["WalletA"]["holding"], 1)
            self.assertEqual(result["WalletB"]["pnl_status"], "holding_only")
            self.assertEqual(result["WalletB"]["holding"], 2)
            self.assertEqual(result["WalletB"]["current_value"], 8)
            self.assertEqual([call[0] for call in calls], ["POST", "GET"])
        finally:
            backend._request_solanatracker_json = original_request
            backend.throttle = original_throttle

    def test_backend_pnl_batch_marks_queued_wallet_as_indexing_with_holding(self):
        backend = import_backend_module()
        original_request = backend._request_solanatracker_json
        try:
            def fake_request(method, url, headers, *, params=None, json_body=None, timeout=10):
                if method == "POST":
                    return {"data": {"indexed": False, "queued": True, "message": "Wallet not yet indexed. Queued for processing."}}, 1
                return {"tokens": [{"address": SOL_MINT, "balance": 5, "value": 15, "price": {"usd": 3}}]}, 1

            backend._request_solanatracker_json = fake_request
            result = backend.fetch_pnl_batch_results(["WalletA"], SOL_MINT, "key")

            self.assertEqual(result["WalletA"]["pnl_status"], "indexing")
            self.assertEqual(result["WalletA"]["holding"], 5)
            self.assertIn("Queued", result["WalletA"]["pnl_message"])
        finally:
            backend._request_solanatracker_json = original_request

    def test_jupiter_quote_uses_v2_order_without_taker(self):
        jupiter_quote = importlib.import_module("jupiter_quote")
        original_get = jupiter_quote.requests.get
        original_throttle = jupiter_quote.throttle
        calls = []
        original_api_key = os.environ.get("JUPITER_API_KEY")

        class Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"outAmount": "12345", "transaction": None, "inUsdValue": "1", "outUsdValue": "0.99"}

        try:
            jupiter_quote.throttle = lambda: calls.append(("throttle", None, None))
            os.environ.pop("JUPITER_API_KEY", None)

            def fake_get(url, params=None, timeout=10):
                calls.append(("get", url, params))
                return Response()

            jupiter_quote.requests.get = fake_get
            out_amount = jupiter_quote.quote_out_amount_raw("InputMint", "OutputMint", 1000)

            self.assertEqual(out_amount, 12345)
            get_call = [call for call in calls if call[0] == "get"][0]
            self.assertTrue(get_call[1].endswith("/swap/v2/order"))
            self.assertEqual(get_call[2]["amount"], 1000)
            self.assertNotIn("taker", get_call[2])
            self.assertNotIn("restrictIntermediateTokens", get_call[2])
        finally:
            jupiter_quote.requests.get = original_get
            jupiter_quote.throttle = original_throttle
            if original_api_key is None:
                os.environ.pop("JUPITER_API_KEY", None)
            else:
                os.environ["JUPITER_API_KEY"] = original_api_key
if __name__ == "__main__":
    unittest.main()
