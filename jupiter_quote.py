import os
import threading
import time
from typing import Any, Dict, Optional

import requests


JUPITER_ORDER_URL = os.getenv("JUPITER_ORDER_URL", "https://api.jup.ag/swap/v2/order")
_lock = threading.Lock()
_last_call = 0.0
_min_interval_seconds = 2.0
_lock_path = os.getenv("JUPITER_RATE_LIMIT_FILE", "/shared/jupiter-rate-limit.lock")


class JupiterQuoteError(RuntimeError):
    pass


def configure_rate_limit(requests_per_second=None):
    global _min_interval_seconds
    try:
        rps = float(requests_per_second)
        if rps <= 0:
            raise ValueError
        _min_interval_seconds = 1.0 / min(rps, 50.0)
    except (TypeError, ValueError):
        _min_interval_seconds = 2.0


configure_rate_limit(os.getenv("JUPITER_REQUESTS_PER_SECOND", "0.5"))


def _local_throttle():
    global _last_call
    now = time.time()
    since = now - _last_call
    if since < _min_interval_seconds:
        time.sleep(_min_interval_seconds - since)
    _last_call = time.time()


def _shared_throttle():
    if os.name != "posix" or not _lock_path:
        return False

    try:
        import fcntl

        directory = os.path.dirname(_lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(_lock_path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                last_call = float(f.read().strip() or "0")
            except ValueError:
                last_call = 0.0

            now = time.time()
            since = now - last_call
            if since < _min_interval_seconds:
                time.sleep(_min_interval_seconds - since)

            f.seek(0)
            f.truncate()
            f.write(str(time.time()))
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except Exception:
        return False


def throttle():
    with _lock:
        if not _shared_throttle():
            _local_throttle()


def _retry_delay(response: Optional[requests.Response], attempt: int) -> float:
    fallback = min(2.0 + attempt, 5.0)
    if response is None:
        return fallback
    reset = response.headers.get("x-ratelimit-reset") or response.headers.get("X-RateLimit-Reset")
    try:
        reset_at = float(reset)
        wait = reset_at - time.time()
        if wait > 0:
            return min(wait, 5.0)
    except (TypeError, ValueError):
        pass
    retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
    try:
        wait = float(retry_after)
        if wait > 0:
            return min(wait, 5.0)
    except (TypeError, ValueError):
        pass
    return fallback


def _float_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    except (TypeError, ValueError):
        return None


def normalized_price_impact(quote: Dict[str, Any]) -> Optional[float]:
    """
    Return price impact as a decimal when possible.
    Jupiter V2 exposes multiple price-impact fields, so prefer USD values.
    """
    in_usd = _float_or_none(quote.get("inUsdValue"))
    out_usd = _float_or_none(quote.get("outUsdValue"))
    if in_usd and in_usd > 0 and out_usd is not None:
        return (out_usd / in_usd) - 1

    price_impact_pct = _float_or_none(quote.get("priceImpactPct"))
    if price_impact_pct is not None:
        return price_impact_pct

    return _float_or_none(quote.get("priceImpact"))


def get_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100, timeout: int = 10) -> Dict[str, Any]:
    try:
        amount_int = int(amount)
    except (TypeError, ValueError):
        raise JupiterQuoteError("Quote amount is invalid")
    if amount_int <= 0:
        raise JupiterQuoteError("Quote amount must be positive")

    params = {
        "inputMint": str(input_mint or "").strip(),
        "outputMint": str(output_mint or "").strip(),
        "amount": amount_int,
        "slippageBps": int(slippage_bps),
    }
    if not params["inputMint"] or not params["outputMint"]:
        raise JupiterQuoteError("Quote mints are required")

    transient_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
    last_error: Any = None
    for attempt in range(1, 4):
        response = None
        try:
            throttle()
            response = requests.get(JUPITER_ORDER_URL, params=params, timeout=timeout)
            if response.status_code in transient_statuses and attempt < 3:
                last_error = f"HTTP {response.status_code}"
                time.sleep(_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            payload = response.json()
            out_amount = int(payload.get("outAmount", "0"))
            if out_amount <= 0:
                raise JupiterQuoteError("Jupiter returned no output amount")
            return payload
        except (requests.RequestException, ValueError, JupiterQuoteError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(_retry_delay(response, attempt))
                continue
            break

    raise JupiterQuoteError(f"Jupiter quote failed: {last_error}")


def quote_out_amount_raw(input_mint: str, output_mint: str, amount: int) -> int:
    quote = get_quote(input_mint, output_mint, amount)
    return int(quote.get("outAmount", "0"))
