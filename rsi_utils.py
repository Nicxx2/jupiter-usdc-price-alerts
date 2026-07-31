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


class InsufficientRsiHistoryError(ValueError):
    """A valid provider response did not contain enough active RSI bars."""

    def __init__(self, message, candles, required_bars, lookback_days):
        super().__init__(message)
        self.candles = candles
        self.active_bars = len(candles)
        self.required_bars = required_bars
        self.lookback_days = lookback_days


class RsiBackfillPendingError(ValueError):
    """A new traded candle exists, but its RSI history is not verified yet."""

    def __init__(self, message, candidate_source, backfill_attempted=False):
        super().__init__(message)
        self.candidate_source = dict(candidate_source or {})
        self.backfill_attempted = bool(backfill_attempted)


def empty_candle_frame():
    return pd.DataFrame(columns=["timestamp", "close", "volume"])


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
    lookback_days: int = 3,
    time_from_override: int = None,
    time_to_override: int = None,
    allow_insufficient: bool = False,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles from SolanaTracker and retain only genuine trading bars.

    Zero-volume rows are not RSI observations. Excluding them before applying
    the response limit keeps sparse tokens stable and means inactivity alone
    cannot create a new RSI value.
    """
    effective_lookback_days = interval_lookback_days(interval, period, lookback_days)
    if time_from_override is None:
        time_from = int((datetime.now(timezone.utc) - timedelta(days=effective_lookback_days)).timestamp())
    else:
        time_from = int(time_from_override)
        if time_from <= 0:
            raise ValueError("RSI history start time must be positive")
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
    if time_to_override is not None:
        params["time_to"] = int(time_to_override)
    throttle()
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()

    df = pd.DataFrame(resp.json().get("oclhv", []))
    required_columns = {"time", "close", "volume"}
    missing = required_columns - set(df.columns)
    if df.empty:
        candles = empty_candle_frame()
        if allow_insufficient:
            return candles
        raise InsufficientRsiHistoryError(
            f"No RSI candles returned in the last {effective_lookback_days} days",
            candles,
            period + 1,
            effective_lookback_days,
        )
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
    # Enforce explicit backfill boundaries locally as well as asking the
    # provider, so an ignored range parameter cannot contaminate the merge.
    if time_from_override is not None:
        df = df.loc[df["time"] >= time_from]
    if time_to_override is not None:
        df = df.loc[df["time"] <= int(time_to_override)]

    if df.empty:
        candles = empty_candle_frame()
        if allow_insufficient:
            return candles
        raise InsufficientRsiHistoryError(
            f"No valid RSI candles returned in the last {effective_lookback_days} days",
            candles,
            period + 1,
            effective_lookback_days,
        )

    df = df.loc[df["volume"] > 0].reset_index(drop=True)
    if df.empty:
        candles = empty_candle_frame()
        if allow_insufficient:
            return candles
        raise InsufficientRsiHistoryError(
            f"No non-zero volume bars in the last {effective_lookback_days} days",
            candles,
            period + 1,
            effective_lookback_days,
        )

    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)

    # Apply the response budget after removing inactive rows so real trades are
    # never displaced by long zero-volume gaps.
    candles = df[["timestamp", "close", "volume"]].tail(fetch_n).reset_index(drop=True)
    if len(candles) < period + 1:
        if allow_insufficient:
            return candles
        raise InsufficientRsiHistoryError(
            f"Not enough traded bars for RSI({period}): {len(candles)}/{period + 1} available",
            candles,
            period + 1,
            effective_lookback_days,
        )
    return candles


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


def normalize_rsi_source(source, interval):
    if not isinstance(source, dict):
        return None
    if str(source.get("calculation_version") or "") != RSI_CALCULATION_VERSION:
        return None
    if str(source.get("interval") or "") != str(interval or ""):
        return None
    try:
        timestamp = pd.Timestamp(source.get("timestamp"))
        close = float(source.get("close"))
        volume = float(source.get("volume"))
        active_bars = int(source.get("active_bars") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    if not math.isfinite(close) or close <= 0 or not math.isfinite(volume) or volume <= 0:
        return None
    return {
        "timestamp": timestamp.isoformat(),
        "close": close,
        "volume": volume,
        "interval": str(interval),
        "active_bars": max(0, active_bars),
        "calculation_version": RSI_CALCULATION_VERSION,
    }


def candle_source(candles, interval):
    if candles is None or candles.empty:
        return None
    last = candles.iloc[-1]
    timestamp = pd.Timestamp(last["timestamp"])
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return {
        "timestamp": timestamp.isoformat(),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
        "interval": str(interval),
        "active_bars": len(candles),
        "calculation_version": RSI_CALCULATION_VERSION,
    }


def source_is_newer_or_revised(candidate, trusted):
    if not candidate or not trusted:
        return False
    candidate_time = pd.Timestamp(candidate["timestamp"])
    trusted_time = pd.Timestamp(trusted["timestamp"])
    if candidate_time > trusted_time:
        return True
    return (
        candidate_time == trusted_time
        and (
            float(candidate["close"]) != float(trusted["close"])
            or float(candidate["volume"]) != float(trusted["volume"])
        )
    )


def merge_rsi_candles(historical, current, fetch_n):
    historical = historical.copy() if historical is not None else empty_candle_frame()
    current = current.copy() if current is not None else empty_candle_frame()
    historical["_priority"] = 0
    current["_priority"] = 1
    merged = pd.concat([historical, current], ignore_index=True)
    if merged.empty:
        return empty_candle_frame()
    return (
        merged.sort_values(["timestamp", "_priority"], kind="stable")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .sort_values("timestamp", kind="stable")
        .tail(fetch_n)
        [["timestamp", "close", "volume"]]
        .reset_index(drop=True)
    )


def get_latest_rsi(
    api_key: str,
    token: str,
    period: int = 14,
    interval: str = "1s",
    lookback_days: int = 3,
    fetch_n: int = 2000,
    include_metadata: bool = False,
    trusted_value=None,
    trusted_source=None,
    pending_source=None,
    allow_backfill=True,
):
    """Fetch candles and return the latest RSI plus optional source metadata."""
    trusted = normalize_rsi_source(trusted_source, interval)
    pending = normalize_rsi_source(pending_source, interval)
    try:
        trusted_number = float(trusted_value)
        if not math.isfinite(trusted_number) or not 0 <= trusted_number <= 100:
            trusted_number = None
    except (TypeError, ValueError):
        trusted_number = None
    if trusted is None:
        trusted_number = None

    backfill_candidate = None
    try:
        df = fetch_candles(
            token=token,
            api_key=api_key,
            interval=interval,
            remove_outliers=True,
            fetch_n=fetch_n,
            period=period,
            lookback_days=lookback_days,
        )
    except InsufficientRsiHistoryError as insufficient:
        current = insufficient.candles
        observed = candle_source(current, interval)

        if pending is not None and (
            observed is None or pd.Timestamp(observed["timestamp"]) < pd.Timestamp(pending["timestamp"])
        ):
            raise RsiBackfillPendingError(
                "A newer traded RSI candle is waiting for enough verified history",
                pending,
            ) from insufficient

        if trusted_number is not None and pending is None and not source_is_newer_or_revised(observed, trusted):
            result = (trusted_number, trusted["timestamp"])
            return result + (trusted,) if include_metadata else result

        candidate = observed or pending
        if trusted is None or candidate is None:
            raise
        if not allow_backfill:
            raise RsiBackfillPendingError(
                "New traded RSI candle detected; waiting for the next protected history retry",
                candidate,
            ) from insufficient

        trusted_time = pd.Timestamp(trusted["timestamp"])
        history_days = interval_lookback_days(interval, period, lookback_days)
        history_from = int((trusted_time - pd.Timedelta(days=history_days)).timestamp())
        history_to = int(trusted_time.timestamp()) + INTERVAL_SECONDS[interval]
        try:
            historical = fetch_candles(
                token=token,
                api_key=api_key,
                interval=interval,
                remove_outliers=True,
                fetch_n=fetch_n,
                period=period,
                lookback_days=lookback_days,
                time_from_override=history_from,
                time_to_override=history_to,
                allow_insufficient=True,
            )
        except Exception as exc:
            raise RsiBackfillPendingError(
                "New traded RSI candle detected; historical candles are temporarily unavailable",
                candidate,
                backfill_attempted=True,
            ) from exc

        df = merge_rsi_candles(historical, current, fetch_n)
        backfill_candidate = candidate
        if len(df) < period + 1:
            raise RsiBackfillPendingError(
                f"New traded RSI candle detected; waiting for enough history ({len(df)}/{period + 1} bars)",
                candidate,
                backfill_attempted=True,
            ) from insufficient

    df["RSI"] = compute_wilder_rsi(df["close"], df["volume"], period).round(2)
    valid = df.dropna(subset=["RSI"])
    if valid.empty:
        if backfill_candidate is not None:
            raise RsiBackfillPendingError(
                f"New traded RSI candle detected; verified history has no directional movement for RSI({period})",
                backfill_candidate,
                backfill_attempted=True,
            )
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
