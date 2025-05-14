# rsi_utils.py

import os
import requests
import pandas as pd
from datetime import datetime

def fetch_candles(token: str,
                  api_key: str,
                  interval: str = "1s",
                  remove_outliers: bool = True,
                  fetch_n: int = 2000) -> pd.DataFrame:
    """
    Fetches oclhv candles from solanatracker.io for the given token.
    Returns a DataFrame with ['timestamp', 'close'] sorted by time.
    """
    url = f"https://data.solanatracker.io/chart/{token}"
    headers = {"x-api-key": api_key}
    params = {
        "type": interval,
        "removeOutliers": str(remove_outliers).lower()
    }

    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()["oclhv"]

    df = pd.DataFrame(data)
    df = df.sort_values("time").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s")
    df["close"] = df["close"].astype(float)
    return df[["timestamp", "close"]].tail(fetch_n).reset_index(drop=True)

def compute_wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Calculates Wilder's RSI exactly:
    1. Seeds with SMA.
    2. Applies recursive smoothing.
    """
    if len(closes) < period + 1:
        raise ValueError(f"Not enough data: need ≥ {period+1} bars, got {len(closes)}")

    delta = closes.diff().fillna(0)
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = pd.Series(index=closes.index, dtype=float)
    avg_loss = pd.Series(index=closes.index, dtype=float)

    avg_gain.iloc[period] = gain.iloc[1:period+1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period+1].mean()

    for i in range(period + 1, len(closes)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    avg_loss = avg_loss.replace(0, 1e-8)
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi.iloc[:period] = float("nan")
    return rsi

def get_latest_rsi(api_key: str, token: str, period: int = 14, interval: str = "1s") -> tuple[float, str]:
    """
    Returns latest RSI value and timestamp as (rsi_value, timestamp_str)
    """
    df = fetch_candles(token, api_key, interval)
    df["RSI"] = compute_wilder_rsi(df["close"], period).round(2)
    latest_row = df.dropna(subset=["RSI"]).iloc[-1]
    return float(latest_row["RSI"]), latest_row["timestamp"].isoformat()
