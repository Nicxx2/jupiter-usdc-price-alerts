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
    def test_fetch_candles_sorts_before_trimming_leading_phantoms(self, mock_get, _mock_throttle):
        from rsi_utils import fetch_candles

        base = 1_700_000_000
        real_rows = [
            {"time": base + i, "open": 100 + i, "high": 100 + i, "low": 100 + i, "close": 100 + i, "volume": 1}
            for i in range(14)
        ]
        recent_phantoms = [
            {"time": base + 14, "open": 999, "high": 999, "low": 999, "close": 999, "volume": 0},
            {"time": base + 15, "open": 999, "high": 999, "low": 999, "close": 999, "volume": 0},
        ]
        mock_get.return_value = FakeResponse({"oclhv": list(reversed(real_rows + recent_phantoms))})

        candles = fetch_candles("TokenMint", "ApiKey", interval="1s", period=14, lookback_days=3)

        self.assertEqual(len(candles), 16)
        self.assertEqual(candles["timestamp"].tolist(), sorted(candles["timestamp"].tolist()))
        self.assertEqual(float(candles["close"].iloc[-1]), 113.0)
        self.assertEqual(float(candles["volume"].iloc[-1]), 0.0)


if __name__ == "__main__":
    unittest.main()