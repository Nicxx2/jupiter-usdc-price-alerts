import requests
import pandas as pd
from datetime import datetime, timedelta

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
    Fetches oclhv candles from solanatracker.io for token,
    going back `lookback_days` (UTC).  Drops only the initial
    zero-volume bars up to the first real trade, then keeps
    all subsequent bars (even if volume==0).  Returns a
    DataFrame with ['timestamp','close'] sorted oldest→newest,
    then keeps only the last `fetch_n` rows.
    """
    time_from = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp())
    url = f"https://data.solanatracker.io/chart/{token}"
    headers = {"x-api-key": api_key}
    params = {
        "type":           interval,
        "removeOutliers": str(remove_outliers).lower(),
        "time_from":      time_from,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json().get("oclhv", []))

    if "volume" in df.columns:
        # drop only the leading phantom bars (volume==0) before first real trade
        nonzero = df[df["volume"] > 0]
        if nonzero.empty:
            raise ValueError(f"No non-zero volume bars in the last {lookback_days} days")
        first_real_idx = nonzero.index[0]
        df = df.loc[first_real_idx:].reset_index(drop=True)

    # ensure enough bars to seed + one period
    if len(df) < period + 1:
        raise ValueError(f"Not enough bars for RSI({period}): got {len(df)}")

    # sort old→new, convert timestamps & close
    df = df.sort_values("time").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s")
    df["close"]     = df["close"].astype(float)

    # only keep the last fetch_n bars
    return df[["timestamp","close"]].tail(fetch_n).reset_index(drop=True)


def compute_wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI:
      1) seed with SMA of the first `period` gains/losses
      2) recursive smoothing thereafter
    """
    if len(closes) < period + 1:
        raise ValueError(f"Not enough data for RSI: need ≥ {period+1} points, got {len(closes)}")

    delta    = closes.diff().fillna(0)
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)

    avg_gain = pd.Series(index=closes.index, dtype=float)
    avg_loss = pd.Series(index=closes.index, dtype=float)

    # 1) seed with SMA
    avg_gain.iloc[period] = gain.iloc[1:period+1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period+1].mean()

    # 2) recursive smoothing
    for i in range(period + 1, len(closes)):
        avg_gain.iloc[i] = (avg_gain.iloc[i-1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i-1] * (period - 1) + loss.iloc[i]) / period

    # avoid div-by-zero
    avg_loss = avg_loss.replace(0, 1e-8)

    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # mask out the first `period` entries
    rsi.iloc[:period] = float("nan")
    return rsi


def get_latest_rsi(
    api_key: str,
    token: str,
    period: int = 14,
    interval: str = "1s",
    lookback_days: int = 3,
    fetch_n: int = 2000
) -> tuple[float, str]:
    """
    Fetches candles, computes RSI over the full series,
    and returns (last_rsi_value, last_rsi_timestamp_iso).
    """
    df = fetch_candles(
        token=token,
        api_key=api_key,
        interval=interval,
        remove_outliers=True,
        fetch_n=fetch_n,
        period=period,
        lookback_days=lookback_days
    )
    df["RSI"] = compute_wilder_rsi(df["close"], period).round(2)
    last = df.dropna(subset=["RSI"]).iloc[-1]
    return float(last["RSI"]), last["timestamp"].isoformat()
