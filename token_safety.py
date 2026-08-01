import math
from typing import Any, Dict

import requests

from solana_rate_limiter import throttle


TOKEN_SAFETY_SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RISKS = 50


class TokenSafetyError(ValueError):
    """The provider response could not produce a trustworthy safety report."""


def _finite_number(value, *, minimum=None, maximum=None):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _nonnegative_int(value):
    number = _finite_number(value, minimum=0)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _clean_text(value, limit):
    if value is None:
        return ""
    text = "".join(ch for ch in str(value) if ch >= " " or ch in "\t\n")
    return " ".join(text.split())[:limit]


def _optional_bool(value):
    return value if isinstance(value, bool) else None


def _risk_level(score, rugged=False):
    if rugged is True:
        return "critical"
    if score <= 3:
        return "low"
    if score <= 6:
        return "medium"
    if score <= 8:
        return "high"
    return "critical"


def _normalize_risks(value):
    if not isinstance(value, list):
        return []
    risks = []
    for raw in value[:MAX_RISKS]:
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("name"), 120)
        if not name:
            continue
        level = _clean_text(raw.get("level"), 20).lower()
        if level not in {"warning", "danger", "info"}:
            level = "unknown"
        item = {
            "name": name,
            "description": _clean_text(raw.get("description"), 500),
            "level": level,
        }
        display_value = _clean_text(raw.get("value"), 80)
        if display_value:
            item["value"] = display_value
        risks.append(item)
    return risks


def _authority_state(pools, field):
    if not pools:
        return "unknown"
    explicit = 0
    malformed = False
    for pool in pools:
        security = pool.get("security") if isinstance(pool, dict) else None
        if not isinstance(security, dict) or field not in security:
            continue
        explicit += 1
        authority = security.get(field)
        if authority is None or (isinstance(authority, str) and not authority.strip()):
            continue
        if isinstance(authority, str):
            return "enabled"
        malformed = True
    if malformed:
        return "unknown"
    if explicit == len(pools):
        return "disabled"
    return "unknown"


def normalize_token_safety(payload: Any, expected_mint: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise TokenSafetyError("Token Safety response is not an object")
    token = payload.get("token")
    risk = payload.get("risk")
    if not isinstance(token, dict) or not isinstance(risk, dict):
        raise TokenSafetyError("Token Safety response is missing token or risk data")

    returned_mint = str(token.get("mint") or "").strip()
    if not returned_mint or returned_mint != str(expected_mint or "").strip():
        raise TokenSafetyError("Token Safety response mint does not match the requested token")

    score = _finite_number(risk.get("score"), minimum=0, maximum=10)
    if score is None:
        raise TokenSafetyError("Token Safety response has an invalid risk score")
    rugged = _optional_bool(risk.get("rugged"))
    jupiter_verified = _optional_bool(risk.get("jupiterVerified"))
    risks = _normalize_risks(risk.get("risks"))

    raw_pools = payload.get("pools")
    raw_pools = raw_pools if isinstance(raw_pools, list) else []
    pools = []
    seen_pool_ids = set()
    liquidity_values = []
    for index, raw_pool in enumerate(raw_pools):
        if not isinstance(raw_pool, dict):
            continue
        pool_id = _clean_text(raw_pool.get("poolId"), 80) or f"index:{index}"
        if pool_id in seen_pool_ids:
            continue
        seen_pool_ids.add(pool_id)
        pools.append(raw_pool)
        liquidity = raw_pool.get("liquidity")
        liquidity_usd = _finite_number(
            liquidity.get("usd") if isinstance(liquidity, dict) else None,
            minimum=0,
        )
        if liquidity_usd is not None:
            liquidity_values.append(liquidity_usd)

    def percentage(path, child=None):
        node = risk.get(path)
        if child is not None:
            node = node.get(child) if isinstance(node, dict) else None
        return _finite_number(node, minimum=0, maximum=100)

    dev = risk.get("dev")
    concentration = {
        "top10": percentage("top10"),
        "developer": _finite_number(
            dev.get("percentage") if isinstance(dev, dict) else None,
            minimum=0,
            maximum=100,
        ),
        "insiders": percentage("insiders", "totalPercentage"),
        "snipers": percentage("snipers", "totalPercentage"),
        "bundlers": percentage("bundlers", "totalPercentage"),
    }

    level = _risk_level(score, rugged)
    danger_conflict = any(item["level"] == "danger" for item in risks) and level in {"low", "medium"}
    holders = _nonnegative_int(payload.get("holders"))
    total_liquidity = sum(liquidity_values) if liquidity_values else None
    if total_liquidity is not None and not math.isfinite(total_liquidity):
        total_liquidity = None
    largest_liquidity = max(liquidity_values) if liquidity_values else None

    return {
        "schema_version": TOKEN_SAFETY_SCHEMA_VERSION,
        "provider": "SolanaTracker",
        "token_mint": returned_mint,
        "score": round(score, 2),
        "level": level,
        "rugged": rugged,
        "jupiter_verified": jupiter_verified,
        "danger_conflict": danger_conflict,
        "risks": risks,
        "authorities": {
            "mint": _authority_state(pools, "mintAuthority"),
            "freeze": _authority_state(pools, "freezeAuthority"),
        },
        "concentration": concentration,
        "holders": holders,
        "pools": {
            "count": len(pools),
            "total_liquidity_usd": round(total_liquidity, 2) if total_liquidity is not None else None,
            "largest_liquidity_usd": round(largest_liquidity, 2) if largest_liquidity is not None else None,
        },
    }


def fetch_token_safety(api_key: str, token_mint: str, base_url="https://data.solanatracker.io", timeout=10):
    if not str(api_key or "").strip():
        raise TokenSafetyError("SolanaTracker API key is not configured")
    mint = str(token_mint or "").strip()
    if not mint:
        raise TokenSafetyError("Token mint is required")

    throttle()
    response = requests.get(
        f"{str(base_url).rstrip('/')}/tokens/{mint}",
        headers={"x-api-key": api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    content_length = response.headers.get("content-length") if hasattr(response, "headers") else None
    try:
        if content_length is not None and int(content_length) > MAX_RESPONSE_BYTES:
            raise TokenSafetyError("Token Safety response is too large")
    except (TypeError, ValueError):
        pass
    content = getattr(response, "content", b"")
    if content and len(content) > MAX_RESPONSE_BYTES:
        raise TokenSafetyError("Token Safety response is too large")
    try:
        payload = response.json()
    except Exception as exc:
        raise TokenSafetyError("Token Safety response is not valid JSON") from exc
    return normalize_token_safety(payload, mint)
