import math
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from solana_rate_limiter import throttle


RSI_CALCULATION_VERSION = "wilder-active-candles-v2"
INTERVAL_SECONDS = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}
MINIMUM_WARMUP_BARS = 120


def interval_lookback_days(interval: str, period: int = 14, minimum_days: int = 3) -> int:
    """Return enough calendar history for stable Wilder smoothing at an interval."""
    seconds = INTERVAL_SECONDS.get(str(interval or "").strip())
    if seconds is None:
        raise ValueError(f"Unsupported RSI interval: {interval}")
    desired_bars = max(period + 1, period + MINIMUM_WARMUP_BARS)
    interval_days = math.ceil((desired_bars * seconds) / 86400)
    return max(1, int(minimum_days), interval_days)


def fetch_candles(
    token: str,
    api_key: str,
    interval: str = "1s",
    remove_outliers: bool = True,
    fetch_n: int = 2000,
    period: int = 14,
    lookback_days: int = 3
) -> pd.DataFrame:
    """
    Fetch OHLCV candles from SolanaTracker and retain only genuine trading bars.

    Zero-volume rows are not RSI observations. Excluding them before applying
    the response limit keeps sparse tokens stable and means inactivity alone
    cannot create a new RSI value.
    """
    effective_lookback_days = interval_lookback_days(interval, period, lookback_days)
    time_from = int((datetime.now(timezone.utc) - timedelta(days=effective_lookback_days)).timestamp())
    url = f"https://data.solanatracker.io/chart/{token}"
    headers = {"x-api-key": api_key}
    params = {
        "type": interval,
        "removeOutliers": str(remove_outliers).lower(),
        "time_from": time_from,
        "currency": "usd",
        "dynamicPools": "true",
        "fastCache": "false",
    }
    throttle()
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()

    df = pd.DataFrame(resp.json().get("oclhv", []))
    required_columns = {"time", "close", "volume"}
    missing = required_columns - set(df.columns)
    if df.empty:
        raise ValueError(f"No RSI candles returned in the last {effective_lookback_days} days")
    if missing:
        raise ValueError(f"RSI candle response missing columns: {', '.join(sorted(missing))}")

    # The API docs do not guarantee row order, so normalize before calculation.
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["time", "close", "volume"])
    finite_rows = (
        df["time"].map(math.isfinite)
        & df["close"].map(math.isfinite)
        & df["volume"].map(math.isfinite)
    )
    df = df.loc[
        finite_rows
        & (df["time"] > 0)
        & (df["close"] > 0)
        & (df["volume"] >= 0)
    ]
    # A response should contain one bar per timestamp. If it does not, prefer
    # the row carrying the most trading volume deterministically.
    df = (
        df.sort_values(["time", "volume"], kind="stable")
        .drop_duplicates(subset=["time"], keep="last")
        .sort_values("time", kind="stable")
        .reset_index(drop=True)
    )

    if df.empty:
        raise ValueError(f"No valid RSI candles returned in the last {effective_lookback_days} days")

    df = df.loc[df["volume"] > 0].reset_index(drop=True)
    if df.empty:
        raise ValueError(f"No non-zero volume bars in the last {effective_lookback_days} days")

    if len(df) < period + 1:
        raise ValueError(f"Not enough traded bars for RSI({period}): {len(df)}/{period + 1} available")

    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)

    # Apply the response budget after removing inactive rows so real trades are
    # never displaced by long zero-volume gaps.
    return df[["timestamp", "close", "volume"]].tail(fetch_n).reset_index(drop=True)


def compute_wilder_rsi(closes: pd.Series, volume: pd.Series = None, period: int = 14) -> pd.Series:
    """
    Wilder's RSI over the supplied active candles:
      1) seed with SMA of the first `period` gains/losses
      2) recursively apply Wilder smoothing thereafter
    """
    if len(closes) < period + 1:
        raise ValueError(f"Not enough data for RSI: need >= {period+1} points, got {len(closes)}")

    delta = closes.diff().fillna(0)
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = pd.Series(index=closes.index, dtype=float)
    avg_loss = pd.Series(index=closes.index, dtype=float)
    rsi = pd.Series(index=closes.index, dtype=float)

    avg_gain.iloc[period] = gain.iloc[1:period+1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period+1].mean()

    for i in range(period + 1, len(closes)):
        avg_gain.iat[i] = (avg_gain.iat[i-1]*(period-1) + gain.iat[i]) / period
        avg_loss.iat[i] = (avg_loss.iat[i-1]*(period-1) + loss.iat[i]) / period

    for i in range(period, len(closes)):
        current_gain = avg_gain.iat[i]
        current_loss = avg_loss.iat[i]
        if current_gain == 0 and current_loss == 0:
            # A completely flat series has no directional momentum. Keep a
            # prior value if one exists; otherwise remain in warm-up.
            rsi.iat[i] = rsi.iat[i - 1] if i > period and pd.notna(rsi.iat[i - 1]) else float("nan")
        elif current_loss == 0:
            rsi.iat[i] = 100.0
        elif current_gain == 0:
            rsi.iat[i] = 0.0
        else:
            rs = current_gain / current_loss
            rsi.iat[i] = 100 - (100 / (1 + rs))

    rsi.iloc[:period] = float("nan")
    return rsi


def get_latest_rsi(
    api_key: str,
    token: str,
    period: int = 14,
    interval: str = "1s",
    lookback_days: int = 3,
    fetch_n: int = 2000,
    include_metadata: bool = False,
):
    """Fetch candles and return the latest RSI plus optional source metadata."""
    df = fetch_candles(
        token=token,
        api_key=api_key,
        interval=interval,
        remove_outliers=True,
        fetch_n=fetch_n,
        period=period,
        lookback_days=lookback_days,
    )
    df["RSI"] = compute_wilder_rsi(df["close"], df["volume"], period).round(2)
    valid = df.dropna(subset=["RSI"])
    if valid.empty:
        raise ValueError(f"No directional movement available for RSI({period})")
    last = valid.iloc[-1]
    result = (float(last["RSI"]), last["timestamp"].isoformat())
    if not include_metadata:
        return result
    return result + ({
        "timestamp": last["timestamp"].isoformat(),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
        "interval": interval,
        "active_bars": len(df),
        "calculation_version": RSI_CALCULATION_VERSION,
    },)
