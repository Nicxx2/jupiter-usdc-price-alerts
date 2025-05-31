import requests
import pandas as pd
from datetime import datetime, timedelta
from solana_rate_limiter import throttle

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
    going back `lookback_days` (UTC). Starts from first real trade,
    includes all bars to present, but forward-fills prices during
    zero-volume periods. Returns a DataFrame with ['timestamp','close', 'volume']
    sorted oldest→newest, then keeps only the last `fetch_n` rows.
    """
    time_from = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp())
    url = f"https://data.solanatracker.io/chart/{token}"
    headers = {"x-api-key": api_key}
    params = {
        "type":           interval,
        "removeOutliers": str(remove_outliers).lower(),
        "time_from":      time_from,
    }
    throttle()
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json().get("oclhv", []))

    if "volume" in df.columns:
        # Find first real trade (remove leading phantoms)
        nonzero = df[df["volume"] > 0]
        if nonzero.empty:
            raise ValueError(f"No non-zero volume bars in the last {lookback_days} days")
        first_real_idx = nonzero.index[0]
        
        # Keep everything from first real trade onwards (including phantoms after last trade)
        df = df.loc[first_real_idx:].reset_index(drop=True)
        
        # Forward-fill prices where volume is 0 (phantom bars)
        # This keeps the last traded price constant during no-trade periods
        mask_no_volume = df["volume"] == 0
        df.loc[mask_no_volume, "close"] = pd.NA
        df["close"] = df["close"].ffill()

    # ensure enough bars to seed + one period
    if len(df) < period + 1:
        raise ValueError(f"Not enough bars for RSI({period}): got {len(df)}")

    # sort old→new, convert timestamps & close
    df = df.sort_values("time").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s")
    df["close"]     = df["close"].astype(float)
    
    # Ensure volume is numeric so `volume.iat[i] == 0` works reliably
    df["volume"] = df["volume"].astype(float)

    # only keep the last fetch_n bars (we need volume too, to skip phantom bars)
    return df[["timestamp","close","volume"]].tail(fetch_n).reset_index(drop=True)


def compute_wilder_rsi(closes: pd.Series, volume: pd.Series, period: int = 14) -> pd.Series:
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
        if volume.iat[i] == 0:
            # phantom bar → freeze
            avg_gain.iat[i] = avg_gain.iat[i-1]
            avg_loss.iat[i] = avg_loss.iat[i-1]
        else:
            avg_gain.iat[i] = (avg_gain.iat[i-1]*(period-1) + gain.iat[i]) / period
            avg_loss.iat[i] = (avg_loss.iat[i-1]*(period-1) + loss.iat[i]) / period

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
    df["RSI"] = compute_wilder_rsi(df["close"], df["volume"], period).round(2)
    last = df.dropna(subset=["RSI"]).iloc[-1]
    return float(last["RSI"]), last["timestamp"].isoformat()