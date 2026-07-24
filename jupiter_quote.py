import os
import threading
import time
from typing import Any, Dict, Optional

import requests


JUPITER_ORDER_URL = os.getenv("JUPITER_ORDER_URL", "https://api.jup.ag/swap/v2/order")
JUPITER_TOKEN_URL = os.getenv("JUPITER_TOKEN_URL", "https://api.jup.ag/tokens/v2/search")
_lock = threading.Lock()
_last_call = 0.0
_min_interval_seconds = 2.0
_community_rules_lock = threading.Lock()
_community_rules_last_call = 0.0
_community_rules_min_interval_seconds = 2.0
_community_rules_lock_path = os.getenv("COMMUNITY_RULES_JUPITER_RATE_LIMIT_FILE", "/shared/community-rules-jupiter-rate-limit.lock")
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


def configure_community_rules_rate_limit(requests_per_second=None):
    global _community_rules_min_interval_seconds
    try:
        rps = float(requests_per_second)
        if rps <= 0:
            raise ValueError
        _community_rules_min_interval_seconds = 1.0 / min(rps, 50.0)
    except (TypeError, ValueError):
        _community_rules_min_interval_seconds = 2.0


configure_rate_limit(os.getenv("JUPITER_REQUESTS_PER_SECOND", "0.5"))
configure_community_rules_rate_limit(os.getenv("COMMUNITY_RULES_JUPITER_REQUESTS_PER_SECOND", "0.5"))


def _local_throttle():
    global _last_call
    now = time.time()
    since = now - _last_call
    if since < _min_interval_seconds:
        time.sleep(_min_interval_seconds - since)
    _last_call = time.time()


def _shared_throttle(lock_path, min_interval_seconds):
    if os.name != "posix" or not lock_path:
        return False

    try:
        import fcntl

        directory = os.path.dirname(lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(lock_path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                last_call = float(f.read().strip() or "0")
            except ValueError:
                last_call = 0.0

            now = time.time()
            since = now - last_call
            if since < min_interval_seconds:
                time.sleep(min_interval_seconds - since)

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
        if not _shared_throttle(_lock_path, _min_interval_seconds):
            _local_throttle()


def _community_rules_local_throttle():
    global _community_rules_last_call
    now = time.time()
    since = now - _community_rules_last_call
    if since < _community_rules_min_interval_seconds:
        time.sleep(_community_rules_min_interval_seconds - since)
    _community_rules_last_call = time.time()


def community_rules_throttle():
    with _community_rules_lock:
        if not _shared_throttle(_community_rules_lock_path, _community_rules_min_interval_seconds):
            _community_rules_local_throttle()


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
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def _community_rules_headers() -> Dict[str, str]:
    api_key = str(os.getenv("JUPITER_API_KEY") or "").strip()
    if not api_key:
        raise JupiterQuoteError("Jupiter API key is not configured")
    return {"x-api-key": api_key}




def price_impact_percent(quote: Dict[str, Any]) -> Optional[float]:
    """Return adverse Jupiter price impact in percentage points (0.5 means 0.5%)."""
    payload = quote if isinstance(quote, dict) else {}
    raw_price_impact = payload.get("priceImpact")
    if raw_price_impact is not None:
        price_impact = _float_or_none(raw_price_impact)
        return abs(price_impact) if price_impact is not None else None

    raw_price_impact_pct = payload.get("priceImpactPct")
    if raw_price_impact_pct is not None:
        price_impact_pct = _float_or_none(raw_price_impact_pct)
        return abs(price_impact_pct) * 100 if price_impact_pct is not None else None
    return None


def normalized_price_impact(quote: Dict[str, Any]) -> Optional[float]:
    """Return Jupiter price impact as a decimal fraction when available."""
    payload = quote if isinstance(quote, dict) else {}
    raw_price_impact = payload.get("priceImpact")
    if raw_price_impact is not None:
        price_impact = _float_or_none(raw_price_impact)
        return price_impact / 100 if price_impact is not None else None

    raw_price_impact_pct = payload.get("priceImpactPct")
    if raw_price_impact_pct is not None:
        return _float_or_none(raw_price_impact_pct)

    return None


def get_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100, timeout: int = 10, community_rules_request: bool = False) -> Dict[str, Any]:
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

    request_headers = _community_rules_headers() if community_rules_request else None
    transient_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
    last_error: Any = None
    for attempt in range(1, 4):
        response = None
        try:
            if community_rules_request:
                community_rules_throttle()
            else:
                throttle()
            request_kwargs = {"params": params, "timeout": timeout}
            if request_headers is not None:
                request_kwargs["headers"] = request_headers
            response = requests.get(JUPITER_ORDER_URL, **request_kwargs)
            if response.status_code in transient_statuses and attempt < 3:
                last_error = f"HTTP {response.status_code}"
                time.sleep(_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise JupiterQuoteError("Jupiter returned an invalid quote response")
            try:
                raw_out_amount = payload.get("outAmount", "0")
                if isinstance(raw_out_amount, bool) or not isinstance(raw_out_amount, (str, int)):
                    raise ValueError
                out_amount = int(raw_out_amount)
            except (TypeError, ValueError):
                raise JupiterQuoteError("Jupiter returned an invalid output amount")
            if out_amount <= 0:
                raise JupiterQuoteError("Jupiter returned no output amount")
            return payload
        except (requests.RequestException, ValueError, JupiterQuoteError) as exc:
            last_error = exc
            retryable = response is None or getattr(response, "status_code", None) in transient_statuses
            if attempt < 3 and retryable:
                time.sleep(_retry_delay(response, attempt))
                continue
            break

    raise JupiterQuoteError(f"Jupiter quote failed: {last_error}")


def get_token_information(mints, timeout: int = 15) -> Dict[str, Dict[str, Any]]:
    unique_mints = list(dict.fromkeys(str(mint or "").strip() for mint in mints if str(mint or "").strip()))
    if not unique_mints:
        return {}
    if len(unique_mints) > 100:
        raise JupiterQuoteError("Jupiter token information supports at most 100 mints per request")

    params = {"query": ",".join(unique_mints)}
    transient_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
    last_error: Any = None
    request_headers = _community_rules_headers()
    for attempt in range(1, 4):
        response = None
        try:
            community_rules_throttle()
            response = requests.get(JUPITER_TOKEN_URL, params=params, headers=request_headers, timeout=timeout)
            if response.status_code in transient_statuses and attempt < 3:
                last_error = f"HTTP {response.status_code}"
                time.sleep(_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise JupiterQuoteError("Jupiter returned an invalid token information response")
            return {
                str(item.get("id")): item
                for item in payload
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
        except (requests.RequestException, ValueError, JupiterQuoteError) as exc:
            last_error = exc
            retryable = response is None or getattr(response, "status_code", None) in transient_statuses
            if attempt < 3 and retryable:
                time.sleep(_retry_delay(response, attempt))
                continue
            break
    raise JupiterQuoteError(f"Jupiter token information failed: {last_error}")


def quote_out_amount_raw(input_mint: str, output_mint: str, amount: int) -> int:
    quote = get_quote(input_mint, output_mint, amount)
    return int(quote.get("outAmount", "0"))
