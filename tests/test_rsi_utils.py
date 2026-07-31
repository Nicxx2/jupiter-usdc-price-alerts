import importlib.util
import unittest
from unittest.mock import patch


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class RsiUtilsTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    @patch("rsi_utils.throttle")
    @patch("rsi_utils.requests.get")
    def test_fetch_candles_sorts_and_removes_inactive_rows_before_budget(self, mock_get, _mock_throttle):
        from rsi_utils import fetch_candles

        base = 1_700_000_000
        real_rows = [
            {"time": base + i, "open": 100 + i, "high": 100 + i, "low": 100 + i, "close": 100 + i, "volume": 1}
            for i in range(15)
        ]
        recent_phantoms = [
            {"time": base + 15, "open": 999, "high": 999, "low": 999, "close": 999, "volume": 0},
            {"time": base + 16, "open": 999, "high": 999, "low": 999, "close": 999, "volume": 0},
        ]
        mock_get.return_value = FakeResponse({"oclhv": list(reversed(real_rows + recent_phantoms))})

        candles = fetch_candles("TokenMint", "ApiKey", interval="1s", period=14, lookback_days=3)

        self.assertEqual(len(candles), 15)
        self.assertEqual(candles["timestamp"].tolist(), sorted(candles["timestamp"].tolist()))
        self.assertEqual(float(candles["close"].iloc[-1]), 114.0)
        self.assertTrue((candles["volume"] > 0).all())

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    @patch("rsi_utils.throttle")
    @patch("rsi_utils.requests.get")
    def test_fetch_candles_reports_traded_bar_progress(self, mock_get, _mock_throttle):
        from rsi_utils import fetch_candles

        base = 1_700_000_000
        mock_get.return_value = FakeResponse({
            "oclhv": [
                {
                    "time": base + i,
                    "open": 100 + i,
                    "high": 100 + i,
                    "low": 100 + i,
                    "close": 100 + i,
                    "volume": 1,
                }
                for i in range(9)
            ]
        })

        with self.assertRaisesRegex(
            ValueError,
            r"Not enough traded bars for RSI\(14\): 9/15 available",
        ):
            fetch_candles("TokenMint", "ApiKey", interval="1s", period=14, lookback_days=3)


    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_interval_lookback_has_enough_wilder_warmup(self):
        from rsi_utils import interval_lookback_days

        self.assertEqual(interval_lookback_days("1s", period=14), 3)
        self.assertGreaterEqual(interval_lookback_days("4h", period=14), 23)
        with self.assertRaises(ValueError):
            interval_lookback_days("invalid", period=14)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_wilder_rsi_matches_reference_and_flat_series_has_no_signal(self):
        import pandas as pd
        from rsi_utils import compute_wilder_rsi

        reference_closes = pd.Series([
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
        ])
        reference_rsi = compute_wilder_rsi(reference_closes, period=14)
        self.assertAlmostEqual(float(reference_rsi.iloc[-1]), 70.46, places=2)

        flat_rsi = compute_wilder_rsi(pd.Series([10.0] * 20), period=14)
        self.assertTrue(flat_rsi.dropna().empty)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_latest_rsi_metadata_identifies_source_candle(self):
        import pandas as pd
        from rsi_utils import get_latest_rsi

        timestamps = pd.date_range("2026-07-30T10:00:00Z", periods=15, freq="min")
        candles = pd.DataFrame({"timestamp": timestamps, "close": range(100, 115), "volume": [1.0] * 15})
        with patch("rsi_utils.fetch_candles", return_value=candles):
            value, timestamp, source = get_latest_rsi("key", "mint", interval="1m", include_metadata=True)

        self.assertEqual(value, 100.0)
        self.assertEqual(source["timestamp"], timestamp)
        self.assertEqual(source["close"], 114.0)
        self.assertEqual(source["volume"], 1.0)
        self.assertEqual(source["interval"], "1m")
        self.assertEqual(source["active_bars"], 15)


    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_long_gap_without_a_new_trade_keeps_trusted_rsi_without_backfill(self):
        import pandas as pd
        from rsi_utils import InsufficientRsiHistoryError, RSI_CALCULATION_VERSION, get_latest_rsi

        trusted_time = "2026-07-20T12:00:00+00:00"
        current = pd.DataFrame({
            "timestamp": [pd.Timestamp(trusted_time)],
            "close": [114.0],
            "volume": [1.0],
        })
        error = InsufficientRsiHistoryError(
            "Not enough traded bars for RSI(14): 1/15 available",
            current,
            15,
            3,
        )
        trusted_source = {
            "timestamp": trusted_time,
            "close": 114.0,
            "volume": 1.0,
            "interval": "1m",
            "active_bars": 15,
            "calculation_version": RSI_CALCULATION_VERSION,
        }

        with patch("rsi_utils.fetch_candles", side_effect=error) as fetch:
            value, timestamp, source = get_latest_rsi(
                "key",
                "mint",
                interval="1m",
                include_metadata=True,
                trusted_value=49.0,
                trusted_source=trusted_source,
            )

        self.assertEqual(value, 49.0)
        self.assertEqual(timestamp, trusted_time)
        self.assertEqual(source, trusted_source)
        fetch.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_new_trade_after_long_gap_uses_bounded_history_and_updates_rsi(self):
        import pandas as pd
        from rsi_utils import InsufficientRsiHistoryError, RSI_CALCULATION_VERSION, get_latest_rsi

        trusted_time = pd.Timestamp("2026-07-20T12:00:00Z")
        new_time = pd.Timestamp("2026-07-24T12:00:00Z")
        historical_times = pd.date_range(end=trusted_time, periods=15, freq="min")
        historical = pd.DataFrame({
            "timestamp": historical_times,
            "close": [float(value) for value in range(100, 115)],
            "volume": [1.0] * 15,
        })
        current = pd.DataFrame({
            "timestamp": [new_time],
            "close": [50.0],
            "volume": [2.0],
        })
        insufficient = InsufficientRsiHistoryError(
            "Not enough traded bars for RSI(14): 1/15 available",
            current,
            15,
            3,
        )
        trusted_source = {
            "timestamp": trusted_time.isoformat(),
            "close": 114.0,
            "volume": 1.0,
            "interval": "1m",
            "active_bars": 15,
            "calculation_version": RSI_CALCULATION_VERSION,
        }

        with patch("rsi_utils.fetch_candles", side_effect=[insufficient, historical]) as fetch:
            value, timestamp, source = get_latest_rsi(
                "key",
                "mint",
                interval="1m",
                include_metadata=True,
                trusted_value=49.0,
                trusted_source=trusted_source,
            )

        self.assertEqual(fetch.call_count, 2)
        backfill = fetch.call_args_list[1].kwargs
        self.assertTrue(backfill["allow_insufficient"])
        self.assertLess(backfill["time_from_override"], int(trusted_time.timestamp()))
        self.assertEqual(backfill["time_to_override"], int(trusted_time.timestamp()) + 60)
        self.assertLess(value, 49.0)
        self.assertEqual(timestamp, new_time.isoformat())
        self.assertEqual(source["close"], 50.0)
        self.assertEqual(source["volume"], 2.0)
        self.assertEqual(source["active_bars"], 16)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_migration_recomputes_from_source_metadata_without_trusting_old_value(self):
        import pandas as pd
        from rsi_utils import InsufficientRsiHistoryError, RSI_CALCULATION_VERSION, get_latest_rsi

        trusted_time = pd.Timestamp("2026-07-20T12:00:00Z")
        historical_times = pd.date_range(end=trusted_time, periods=15, freq="min")
        historical = pd.DataFrame({
            "timestamp": historical_times,
            "close": [float(value) for value in range(100, 115)],
            "volume": [1.0] * 15,
        })
        current = historical.tail(1).reset_index(drop=True)
        insufficient = InsufficientRsiHistoryError(
            "Not enough traded bars for RSI(14): 1/15 available",
            current,
            15,
            3,
        )
        source = {
            "timestamp": trusted_time.isoformat(),
            "close": 114.0,
            "volume": 1.0,
            "interval": "1m",
            "active_bars": 15,
            "calculation_version": RSI_CALCULATION_VERSION,
        }

        with patch("rsi_utils.fetch_candles", side_effect=[insufficient, historical]) as fetch:
            value, timestamp, refreshed_source = get_latest_rsi(
                "key",
                "mint",
                interval="1m",
                include_metadata=True,
                trusted_value=None,
                trusted_source=source,
            )

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(value, 100.0)
        self.assertEqual(timestamp, trusted_time.isoformat())
        self.assertEqual(refreshed_source["calculation_version"], RSI_CALCULATION_VERSION)
    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_new_trade_hides_old_rsi_when_bounded_history_is_unavailable(self):
        import pandas as pd
        from rsi_utils import (
            InsufficientRsiHistoryError,
            RSI_CALCULATION_VERSION,
            RsiBackfillPendingError,
            get_latest_rsi,
        )

        trusted_time = pd.Timestamp("2026-07-20T12:00:00Z")
        new_time = pd.Timestamp("2026-07-24T12:00:00Z")
        current = pd.DataFrame({
            "timestamp": [new_time],
            "close": [50.0],
            "volume": [2.0],
        })
        insufficient = InsufficientRsiHistoryError(
            "Not enough traded bars for RSI(14): 1/15 available",
            current,
            15,
            3,
        )
        trusted_source = {
            "timestamp": trusted_time.isoformat(),
            "close": 114.0,
            "volume": 1.0,
            "interval": "1m",
            "active_bars": 15,
            "calculation_version": RSI_CALCULATION_VERSION,
        }

        with patch("rsi_utils.fetch_candles", side_effect=[insufficient, RuntimeError("provider unavailable")]) as fetch:
            with self.assertRaises(RsiBackfillPendingError) as caught:
                get_latest_rsi(
                    "key",
                    "mint",
                    interval="1m",
                    include_metadata=True,
                    trusted_value=49.0,
                    trusted_source=trusted_source,
                )

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(caught.exception.candidate_source["timestamp"], new_time.isoformat())
        self.assertEqual(caught.exception.candidate_source["close"], 50.0)
        self.assertTrue(caught.exception.backfill_attempted)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_backfill_cooldown_uses_one_normal_request_and_tracks_newer_candidate(self):
        import pandas as pd
        from rsi_utils import (
            InsufficientRsiHistoryError,
            RSI_CALCULATION_VERSION,
            RsiBackfillPendingError,
            get_latest_rsi,
        )

        trusted_time = pd.Timestamp("2026-07-20T12:00:00Z")
        pending_time = pd.Timestamp("2026-07-23T12:00:00Z")
        newest_time = pd.Timestamp("2026-07-24T12:00:00Z")
        current = pd.DataFrame({
            "timestamp": [newest_time],
            "close": [48.0],
            "volume": [3.0],
        })
        insufficient = InsufficientRsiHistoryError(
            "Not enough traded bars for RSI(14): 1/15 available",
            current,
            15,
            3,
        )
        trusted_source = {
            "timestamp": trusted_time.isoformat(),
            "close": 114.0,
            "volume": 1.0,
            "interval": "1m",
            "active_bars": 15,
            "calculation_version": RSI_CALCULATION_VERSION,
        }
        pending_source = {
            **trusted_source,
            "timestamp": pending_time.isoformat(),
            "close": 50.0,
            "volume": 2.0,
            "active_bars": 1,
        }

        with patch("rsi_utils.fetch_candles", side_effect=insufficient) as fetch:
            with self.assertRaises(RsiBackfillPendingError) as caught:
                get_latest_rsi(
                    "key",
                    "mint",
                    interval="1m",
                    include_metadata=True,
                    trusted_value=49.0,
                    trusted_source=trusted_source,
                    pending_source=pending_source,
                    allow_backfill=False,
                )

        fetch.assert_called_once()
        self.assertFalse(caught.exception.backfill_attempted)
        self.assertEqual(caught.exception.candidate_source["timestamp"], newest_time.isoformat())
        self.assertEqual(caught.exception.candidate_source["close"], 48.0)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_complete_normal_history_resolves_even_during_backfill_cooldown(self):
        import pandas as pd
        from rsi_utils import get_latest_rsi

        timestamps = pd.date_range("2026-07-30T10:00:00Z", periods=15, freq="min")
        candles = pd.DataFrame({"timestamp": timestamps, "close": range(100, 115), "volume": [1.0] * 15})
        with patch("rsi_utils.fetch_candles", return_value=candles) as fetch:
            value, timestamp, source = get_latest_rsi(
                "key",
                "mint",
                interval="1m",
                include_metadata=True,
                allow_backfill=False,
            )

        fetch.assert_called_once()
        self.assertEqual(value, 100.0)
        self.assertEqual(timestamp, timestamps[-1].isoformat())
        self.assertEqual(source["active_bars"], 15)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_flat_bounded_history_is_a_protected_backfill_failure(self):
        import pandas as pd
        from rsi_utils import (
            InsufficientRsiHistoryError,
            RSI_CALCULATION_VERSION,
            RsiBackfillPendingError,
            get_latest_rsi,
        )

        trusted_time = pd.Timestamp("2026-07-20T12:00:00Z")
        new_time = pd.Timestamp("2026-07-24T12:00:00Z")
        historical = pd.DataFrame({
            "timestamp": pd.date_range(end=trusted_time, periods=15, freq="min"),
            "close": [100.0] * 15,
            "volume": [1.0] * 15,
        })
        current = pd.DataFrame({"timestamp": [new_time], "close": [100.0], "volume": [2.0]})
        insufficient = InsufficientRsiHistoryError("insufficient", current, 15, 3)
        trusted_source = {
            "timestamp": trusted_time.isoformat(),
            "close": 100.0,
            "volume": 1.0,
            "interval": "1m",
            "active_bars": 15,
            "calculation_version": RSI_CALCULATION_VERSION,
        }

        with patch("rsi_utils.fetch_candles", side_effect=[insufficient, historical]) as fetch:
            with self.assertRaises(RsiBackfillPendingError) as caught:
                get_latest_rsi(
                    "key",
                    "mint",
                    interval="1m",
                    trusted_value=50.0,
                    trusted_source=trusted_source,
                )

        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(caught.exception.backfill_attempted)
        self.assertIn("no directional movement", str(caught.exception))

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for rsi_utils")
    def test_current_candle_revision_wins_when_backfill_overlaps(self):
        import pandas as pd
        from rsi_utils import merge_rsi_candles

        timestamp = pd.Timestamp("2026-07-20T12:00:00Z")
        historical = pd.DataFrame({"timestamp": [timestamp], "close": [100.0], "volume": [1.0]})
        current = pd.DataFrame({"timestamp": [timestamp], "close": [90.0], "volume": [3.0]})

        merged = merge_rsi_candles(historical, current, fetch_n=2000)

        self.assertEqual(len(merged), 1)
        self.assertEqual(float(merged.iloc[0]["close"]), 90.0)
        self.assertEqual(float(merged.iloc[0]["volume"]), 3.0)
if __name__ == "__main__":
    unittest.main()
