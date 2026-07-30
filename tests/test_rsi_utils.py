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


if __name__ == "__main__":
    unittest.main()