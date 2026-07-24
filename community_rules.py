import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


RULE_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "min_holders": {
        "label": "Minimum holders",
        "operator": ">=",
        "unit": "count",
        "description": "Jupiter-reported token holder accounts.",
    },
    "min_market_cap": {
        "label": "Minimum market cap",
        "operator": ">=",
        "unit": "usd",
        "description": "Jupiter-reported circulating market cap in USD.",
    },
    "min_liquidity": {
        "label": "Minimum liquidity",
        "operator": ">=",
        "unit": "usd",
        "description": "Jupiter-reported total token liquidity in USD.",
    },
    "min_volume_24h": {
        "label": "Minimum 24h volume",
        "operator": ">=",
        "unit": "usd",
        "description": "Jupiter 24-hour buy volume plus sell volume.",
    },
    "max_sell_pressure_24h": {
        "label": "Maximum 24h sell pressure",
        "operator": "<=",
        "unit": "percent",
        "description": "Sell volume as a percentage of total 24-hour volume.",
    },
    "min_volume_liquidity_ratio": {
        "label": "Minimum volume / liquidity",
        "operator": ">=",
        "unit": "ratio",
        "description": "24-hour total volume divided by current liquidity.",
    },
    "max_price_impact": {
        "label": "Maximum price impact",
        "operator": "<=",
        "unit": "percent",
        "description": "Jupiter sell-quote price impact for the configured scenario.",
    },
}

RULE_ORDER = tuple(RULE_DEFINITIONS)
ALERT_MODES = {"once", "rearm"}
SELL_AMOUNT_MODES = {"tracked_usdc", "token_amount"}
CONFIRMATION_SAMPLES = 2


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_number(value: Any) -> Optional[float]:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _nonnegative_int(value: Any) -> int:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return 0
    return int(number)


def _parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def default_rules_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "alert_enabled": False,
        "alert_mode": "once",
        "sell_amount_mode": "tracked_usdc",
        "sell_token_amount": None,
        "items": [],
    }


def normalize_rules_config(raw: Any, *, strict: bool = False) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    config = default_rules_config()
    config["enabled"] = _coerce_bool(source.get("enabled"), False)
    config["alert_enabled"] = _coerce_bool(source.get("alert_enabled"), False)

    alert_mode = str(source.get("alert_mode") or "once").strip().lower()
    if alert_mode not in ALERT_MODES:
        if strict:
            raise ValueError("Rule alert mode must be once or rearm")
        alert_mode = "once"
    config["alert_mode"] = alert_mode

    amount_mode = str(source.get("sell_amount_mode") or "tracked_usdc").strip().lower()
    if amount_mode not in SELL_AMOUNT_MODES:
        if strict:
            raise ValueError("Sell amount mode must use tracked USDC or a token amount")
        amount_mode = "tracked_usdc"
    config["sell_amount_mode"] = amount_mode

    sell_amount = _finite_number(source.get("sell_token_amount"))
    if sell_amount is not None and sell_amount <= 0:
        sell_amount = None
    config["sell_token_amount"] = sell_amount

    items: List[Dict[str, Any]] = []
    seen = set()
    raw_items = source.get("items") if isinstance(source.get("items"), list) else []
    if strict and len(raw_items) > len(RULE_DEFINITIONS):
        raise ValueError("Too many action rules")

    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            if strict:
                raise ValueError("Every action rule must be an object")
            continue
        rule_type = str(raw_item.get("type") or "").strip()
        if rule_type not in RULE_DEFINITIONS:
            if strict:
                raise ValueError(f"Unsupported action rule: {rule_type or 'missing type'}")
            continue
        if rule_type in seen:
            if strict:
                raise ValueError(f"Only one {RULE_DEFINITIONS[rule_type]['label']} rule is allowed")
            continue

        target = _finite_number(raw_item.get("target"))
        if target is None:
            if strict:
                raise ValueError(f"{RULE_DEFINITIONS[rule_type]['label']} needs a valid target")
            continue
        if rule_type == "min_holders":
            if target < 1 or not float(target).is_integer():
                if strict:
                    raise ValueError("Minimum holders must be a whole number of at least 1")
                continue
            target = int(target)
        elif rule_type in {"max_sell_pressure_24h", "max_price_impact"}:
            if target < 0 or target > 100:
                if strict:
                    raise ValueError(f"{RULE_DEFINITIONS[rule_type]['label']} must be between 0 and 100")
                continue
        elif target <= 0:
            if strict:
                raise ValueError(f"{RULE_DEFINITIONS[rule_type]['label']} must be greater than zero")
            continue

        seen.add(rule_type)
        items.append({
            "type": rule_type,
            "enabled": _coerce_bool(raw_item.get("enabled"), True),
            "target": target,
        })

    items.sort(key=lambda item: RULE_ORDER.index(item["type"]))
    config["items"] = items

    if strict and config["enabled"] and not any(item["enabled"] for item in items):
        raise ValueError("Enable at least one action rule before turning the rules card on")
    if strict and any(item["enabled"] and item["type"] == "max_price_impact" for item in items):
        if amount_mode == "token_amount" and sell_amount is None:
            raise ValueError("Maximum price impact needs a valid custom token amount")
    return config


def rules_config_signature(config: Any) -> str:
    normalized = normalize_rules_config(config)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def enabled_rule_types(config: Any) -> List[str]:
    normalized = normalize_rules_config(config)
    if not normalized["enabled"]:
        return []
    return [item["type"] for item in normalized["items"] if item["enabled"]]


def rules_require_price_impact(config: Any) -> bool:
    return "max_price_impact" in enabled_rule_types(config)


def _result_item(rule_type: str, target: float, current: Optional[float], reason: str = "") -> Dict[str, Any]:
    definition = RULE_DEFINITIONS[rule_type]
    status = "unknown"
    if current is not None:
        if definition["operator"] == ">=":
            status = "pass" if current >= target else "fail"
        else:
            status = "pass" if current <= target else "fail"
    return {
        "type": rule_type,
        "label": definition["label"],
        "description": definition["description"],
        "operator": definition["operator"],
        "unit": definition["unit"],
        "target": target,
        "current": current,
        "status": status,
        "reason": reason if status == "unknown" else "",
    }


def evaluate_rules(
    config: Any,
    token_info: Optional[Dict[str, Any]],
    price_impact: Optional[Dict[str, Any]],
    *,
    evaluated_at: Optional[str] = None,
    fetch_error: str = "",
) -> Dict[str, Any]:
    normalized = normalize_rules_config(config)
    now = evaluated_at or datetime.now(timezone.utc).isoformat()
    signature = rules_config_signature(normalized)

    if not normalized["enabled"]:
        return {
            "status": "disabled",
            "items": [],
            "enabled_count": 0,
            "evaluated_at": now,
            "source_updated_at": None,
            "fetch_error": "",
            "config_signature": signature,
        }

    active_items = [item for item in normalized["items"] if item["enabled"]]
    if not active_items:
        return {
            "status": "not_configured",
            "items": [],
            "enabled_count": 0,
            "evaluated_at": now,
            "source_updated_at": None,
            "fetch_error": "",
            "config_signature": signature,
        }

    info = token_info if isinstance(token_info, dict) else {}
    stats = info.get("stats24h") if isinstance(info.get("stats24h"), dict) else {}
    holders = _nonnegative_number(info.get("holderCount"))
    if holders is not None and not holders.is_integer():
        holders = None
    market_cap = _nonnegative_number(info.get("mcap"))
    liquidity = _nonnegative_number(info.get("liquidity"))
    buy_volume = _nonnegative_number(stats.get("buyVolume"))
    sell_volume = _nonnegative_number(stats.get("sellVolume"))
    total_volume = None
    if buy_volume is not None and sell_volume is not None:
        total_volume = _finite_number(buy_volume + sell_volume)

    sell_pressure = None
    if total_volume is not None and total_volume > 0 and sell_volume is not None:
        sell_pressure = _finite_number((sell_volume / total_volume) * 100)

    volume_liquidity_ratio = None
    if total_volume is not None and liquidity is not None and liquidity > 0:
        volume_liquidity_ratio = _finite_number(total_volume / liquidity)

    impact_value = None
    impact_reason = "Waiting for a fresh Jupiter sell quote"
    if isinstance(price_impact, dict):
        impact_value = _finite_number(price_impact.get("value"))
        if price_impact.get("stale"):
            impact_value = None
            impact_reason = "The latest sell quote is stale"
        elif price_impact.get("error"):
            impact_value = None
            impact_reason = str(price_impact.get("error"))[:180]
        elif impact_value is not None and impact_value < 0:
            impact_value = None
            impact_reason = "Jupiter returned an invalid negative price impact"

    values = {
        "min_holders": holders,
        "min_market_cap": market_cap,
        "min_liquidity": liquidity,
        "min_volume_24h": total_volume,
        "max_sell_pressure_24h": sell_pressure,
        "min_volume_liquidity_ratio": volume_liquidity_ratio,
        "max_price_impact": impact_value,
    }
    missing_reasons = {
        "min_holders": "Jupiter did not report a holder count",
        "min_market_cap": "Jupiter did not report market cap",
        "min_liquidity": "Jupiter did not report liquidity",
        "min_volume_24h": "Jupiter did not report complete 24-hour volume",
        "max_sell_pressure_24h": "Sell pressure needs non-zero 24-hour buy and sell volume",
        "min_volume_liquidity_ratio": "The ratio needs complete 24-hour volume and positive liquidity",
        "max_price_impact": impact_reason,
    }

    results = []
    for item in normalized["items"]:
        definition = RULE_DEFINITIONS[item["type"]]
        if not item["enabled"]:
            results.append({
                "type": item["type"],
                "label": definition["label"],
                "description": definition["description"],
                "operator": definition["operator"],
                "unit": definition["unit"],
                "target": item["target"],
                "current": None,
                "status": "disabled",
                "reason": "",
            })
            continue
        current = values[item["type"]]
        reason = (fetch_error or missing_reasons[item["type"]]) if current is None else ""
        results.append(_result_item(item["type"], item["target"], current, reason))

    enabled_results = [item for item in results if item["status"] != "disabled"]
    if any(item["status"] == "fail" for item in enabled_results):
        overall = "fail"
    elif any(item["status"] == "unknown" for item in enabled_results):
        overall = "unknown"
    else:
        overall = "pass"

    runtime = {
        "status": overall,
        "items": results,
        "enabled_count": len(enabled_results),
        "evaluated_at": now,
        "source_updated_at": info.get("updatedAt"),
        "fetched_at": now if token_info is not None else None,
        "fetch_error": str(fetch_error or "")[:180],
        "config_signature": signature,
    }
    if isinstance(price_impact, dict):
        runtime["price_impact"] = dict(price_impact)
    return runtime


def advance_alert_state(
    previous_runtime: Any,
    evaluation: Dict[str, Any],
    config: Any,
    *,
    now: Optional[str] = None,
    max_gap_seconds: Optional[float] = None,
) -> Tuple[Dict[str, Any], bool]:
    normalized = normalize_rules_config(config)
    previous = previous_runtime if isinstance(previous_runtime, dict) else {}
    signature = evaluation.get("config_signature") or rules_config_signature(normalized)
    same_config = previous.get("config_signature") == signature

    previous_alert = previous.get("alert_state") if same_config and isinstance(previous.get("alert_state"), dict) else {}
    alert_state = {
        "armed": _coerce_bool(previous_alert.get("armed"), True),
        "fired": _coerce_bool(previous_alert.get("fired"), False),
        "last_sent_at": previous_alert.get("last_sent_at"),
        "last_delivery_error": previous_alert.get("last_delivery_error"),
    }
    pass_streak = _nonnegative_int(previous.get("pass_streak")) if same_config else 0
    fail_streak = _nonnegative_int(previous.get("fail_streak")) if same_config else 0
    if same_config and max_gap_seconds is not None:
        previous_at = _parse_utc(previous.get("evaluated_at"))
        current_at = _parse_utc(now or evaluation.get("evaluated_at"))
        try:
            maximum_gap = max(0.0, float(max_gap_seconds))
        except (TypeError, ValueError, OverflowError):
            maximum_gap = 0.0
        gap = (current_at - previous_at).total_seconds() if previous_at and current_at else None
        if gap is None or gap < 0 or gap > maximum_gap:
            pass_streak = 0
            fail_streak = 0

    status = evaluation.get("status")
    if status == "pass":
        pass_streak += 1
        fail_streak = 0
    elif status == "fail":
        fail_streak += 1
        pass_streak = 0
    else:
        pass_streak = 0
        fail_streak = 0

    if normalized["alert_mode"] == "rearm" and fail_streak >= CONFIRMATION_SAMPLES:
        alert_state["armed"] = True
    if normalized["alert_mode"] == "once" and alert_state["fired"]:
        alert_state["armed"] = False

    confirmed_ready = status == "pass" and pass_streak >= CONFIRMATION_SAMPLES
    should_send = bool(
        normalized["enabled"]
        and normalized["alert_enabled"]
        and confirmed_ready
        and alert_state["armed"]
    )

    next_runtime = dict(evaluation)
    next_runtime.update({
        "pass_streak": pass_streak,
        "fail_streak": fail_streak,
        "confirmed_ready": confirmed_ready,
        "confirmation_required": CONFIRMATION_SAMPLES,
        "alert_state": alert_state,
        "config_signature": signature,
        "alert_mode": normalized["alert_mode"],
        "alert_enabled": normalized["alert_enabled"],
    })
    return next_runtime, should_send


def mark_alert_delivery(runtime: Dict[str, Any], *, success: bool, error: str = "", sent_at: Optional[str] = None) -> Dict[str, Any]:
    next_runtime = dict(runtime)
    alert_state = dict(next_runtime.get("alert_state") or {})
    if success:
        alert_state.update({
            "armed": False,
            "fired": True,
            "last_sent_at": sent_at or datetime.now(timezone.utc).isoformat(),
            "last_delivery_error": None,
        })
    else:
        alert_state["last_delivery_error"] = str(error or "Notification delivery failed")[:180]
    next_runtime["alert_state"] = alert_state
    return next_runtime


def stale_rules_state(runtime: Any, config: Any, *, stale_after_seconds: int, now: Optional[datetime] = None, global_enabled: bool = True) -> Dict[str, Any]:
    normalized = normalize_rules_config(config)
    source = dict(runtime) if isinstance(runtime, dict) else {}
    if not global_enabled:
        return {
            **source,
            "status": "global_disabled",
            "items": [],
            "confirmed_ready": False,
            "stale": False,
            "fetch_error": "",
        }
    if not normalized["enabled"]:
        return {
            **source,
            "status": "disabled",
            "items": source.get("items") or [],
            "stale": False,
        }
    if not any(item["enabled"] for item in normalized["items"]):
        return {
            **source,
            "status": "not_configured",
            "items": source.get("items") or [],
            "stale": False,
        }

    evaluated_at = source.get("evaluated_at")
    freshness_at = evaluated_at or (source.get("status_updated_at") if source.get("status") == "waiting" else None)
    try:
        evaluated = datetime.fromisoformat(str(freshness_at).replace("Z", "+00:00"))
        if evaluated.tzinfo is None:
            evaluated = evaluated.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - evaluated.astimezone(timezone.utc)).total_seconds()
    except Exception:
        age = float("inf")

    if age <= max(1, stale_after_seconds):
        source["stale"] = False
        return source

    stale_items = []
    for item in source.get("items") or []:
        next_item = dict(item)
        if next_item.get("status") not in {"disabled"}:
            next_item["status"] = "unknown"
            next_item["reason"] = "Rule data is stale"
        stale_items.append(next_item)
    source.update({
        "status": "unknown",
        "items": stale_items,
        "confirmed_ready": False,
        "stale": True,
        "fetch_error": source.get("fetch_error") or "Rule data is stale",
    })
    return source


def rules_alert_message(token_name: str, mint: str, runtime: Dict[str, Any]) -> str:
    lines = [f"All enabled action rules are passing for {token_name}."]
    for item in runtime.get("items") or []:
        if item.get("status") == "disabled":
            continue
        current = item.get("current")
        lines.append(f"- {item.get('label')}: {current} {item.get('operator')} {item.get('target')}")
    lines.extend([
        f"Checked: {runtime.get('evaluated_at') or 'unknown'}",
        f"Token: {mint}",
        "Status reflects a market snapshot and is not an execution guarantee.",
    ])
    return "\n".join(lines)
