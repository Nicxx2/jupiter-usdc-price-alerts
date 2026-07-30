import math
import os
import time
import threading
import requests
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from statistics import median
from jupiter_quote import JupiterQuoteError, get_quote, get_token_information, price_impact_percent
from community_rules import (
    advance_alert_state,
    evaluate_rules,
    mark_alert_delivery,
    normalize_rules_config,
    rules_alert_message,
    rules_config_signature,
    rules_require_price_impact,
)
from rule_history import record_rule_history
from rsi_utils import get_latest_rsi
from solana_rate_limiter import configure_rate_limit
from typing import Dict, Any, Optional

def coerce_int(value, default, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def coerce_float(value, default, minimum=None, maximum=None):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not (number == number) or number in (float("inf"), float("-inf")):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def normalize_rate_limit_mode(value, requests_per_second=None):
    mode = str(value or "").strip().lower()
    if mode in {"off", "disabled", "none"}:
        return "off"
    if mode in {"custom", "safe"}:
        return mode
    try:
        if float(requests_per_second) != 1.0:
            return "custom"
    except (TypeError, ValueError):
        pass
    return "safe"


def effective_rate_limit_rps(mode, requests_per_second):
    if normalize_rate_limit_mode(mode) == "safe":
        return 1.0
    return coerce_float(requests_per_second, 1.0, minimum=0.1, maximum=50.0)


def apply_solanatracker_rate_limit():
    global SOLANATRACKER_RATE_LIMIT_MODE
    SOLANATRACKER_RATE_LIMIT_MODE = normalize_rate_limit_mode(SOLANATRACKER_RATE_LIMIT_MODE, SOLANATRACKER_REQUESTS_PER_SECOND)
    configure_rate_limit(
        effective_rate_limit_rps(SOLANATRACKER_RATE_LIMIT_MODE, SOLANATRACKER_REQUESTS_PER_SECOND),
        enabled=SOLANATRACKER_RATE_LIMIT_MODE != "off",
    )


def env_int(name, default, minimum=None, maximum=None):
    return coerce_int(os.getenv(name), default, minimum, maximum)


def env_float(name, default, minimum=None, maximum=None):
    return coerce_float(os.getenv(name), default, minimum, maximum)


INPUT_MINT = os.getenv("INPUT_MINT")
OUTPUT_MINT = os.getenv("OUTPUT_MINT")
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 60, minimum=5)

SOLANATRACKER_RATE_LIMIT_MODE = normalize_rate_limit_mode(os.getenv("SOLANATRACKER_RATE_LIMIT_MODE"), os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND"))
SOLANATRACKER_REQUESTS_PER_SECOND = env_float("SOLANATRACKER_REQUESTS_PER_SECOND", 1.0, minimum=0.1, maximum=50.0)
SOLANATRACKER_FEATURES_ENABLED = coerce_bool(os.getenv("SOLANATRACKER_ENABLED", os.getenv("SOLANATRACKER_FEATURES_ENABLED")), True)
apply_solanatracker_rate_limit()

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

shared_json_path = os.getenv("SHARED_STATE_PATH", "/shared/jupiter-latest.json")
config_json_path = os.getenv("CONFIG_PATH", "/shared/config.json")

NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC_ALPHABET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
ACTIVE_TOKEN_CONFIG: Dict[str, Any] = {}

USD_AMOUNT = env_float("USD_AMOUNT", 100.0, minimum=0.000001)
BUY_ALERTS = []
SELL_ALERTS = []
ALERT_RESET_MINUTES = env_int("ALERT_RESET_MINUTES", 0, minimum=0)

# How often to run RSI logic (in minutes)
RSI_CHECK_INTERVAL = env_int("RSI_CHECK_INTERVAL", 4, minimum=1)
_last_rsi_at: datetime | None = None

last_buy_alert = {}
last_sell_alert = {}
TOKEN_CHANGED_SINCE_LAST_WRITE = False
TOKEN_RUNTIMES: Dict[str, Dict[str, Any]] = {}
SCHEDULER_LAST_CHECK: Dict[str, float] = {}
SCHEDULER_TOKEN_NONCES: Dict[str, str] = {}
SCHEDULER_CURSOR = 0
PRICE_CONFIRMATIONS: Dict[str, Dict[str, Any]] = {}
PRICE_MOVE_CONFIRMATION_ENABLED = coerce_bool(os.getenv("PRICE_MOVE_CONFIRMATION_ENABLED"), True)
PRICE_MOVE_CONFIRMATION_THRESHOLD_PERCENT = env_float(
    "PRICE_MOVE_CONFIRMATION_THRESHOLD_PERCENT", 50.0, minimum=5.0, maximum=1000.0
)
PRICE_MOVE_CONFIRMATION_TOLERANCE_PERCENT = env_float(
    "PRICE_MOVE_CONFIRMATION_TOLERANCE_PERCENT", 15.0, minimum=1.0, maximum=100.0
)
PRICE_MOVE_CONFIRMATION_DELAY_SECONDS = env_int(
    "PRICE_MOVE_CONFIRMATION_DELAY_SECONDS", 5, minimum=5, maximum=60
)
PRICE_MOVE_CONFIRMATION_MAX_SAMPLES = 3
PRICE_MOVE_CONFIRMATION_BASELINE_POINTS = 7
PRICE_MOVE_CONFIRMATION_MIN_BASELINE_POINTS = 3
COMMUNITY_RULES_CHECK_INTERVAL = env_int("COMMUNITY_RULES_CHECK_INTERVAL", 120, minimum=60, maximum=3600)
COMMUNITY_RULES_QUOTE_MAX_AGE_SECONDS = env_int("COMMUNITY_RULES_QUOTE_MAX_AGE_SECONDS", 90, minimum=30, maximum=3600)
COMMUNITY_RULES_SOURCE_MAX_AGE_SECONDS = env_int("COMMUNITY_RULES_SOURCE_MAX_AGE_SECONDS", 3600, minimum=300, maximum=86400)
LAST_COMMUNITY_RULES_REFRESH = 0.0
LAST_COMMUNITY_RULES_CONFIG_SIGNATURE = ""
QUOTE_CACHE: Dict[tuple, Dict[str, Any]] = {}
ACTIVE_RSI_REFRESH_NONCE = ""

PRICE_HISTORY_RETENTION_HOURS = 24
PRICE_HISTORY_MAX_POINTS_PER_TOKEN = 3000

# RSI config
SOLANATRACKER_API_KEY = os.getenv("SOLANATRACKER_API_KEY")
RSI_INTERVAL = os.getenv("RSI_INTERVAL", "1s")
RSI_ALERTS_RAW = os.getenv("RSI_ALERTS", "")
RSI_STATE = {}  # format: {'above:70': {"triggered": False}, ...}
# RSI reset mode (true=allow re-trigger on cross-back).
RSI_RESET_ENABLED = coerce_bool(os.getenv("RSI_RESET_ENABLED"), False)
RSI_ENABLED = coerce_bool(os.getenv("RSI_ENABLED"), True)


def solanatracker_api_key_configured():
    return bool(SOLANATRACKER_API_KEY)


def solanatracker_effective_enabled():
    return bool(SOLANATRACKER_FEATURES_ENABLED) and solanatracker_api_key_configured()


def _optional_int(value):
    try:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_decimals(value):
    decimals = _optional_int(value)
    if decimals is None or decimals < 0 or decimals > 12:
        return None
    return decimals


def normalize_ntfy_topic(value):
    if value is None:
        return ""
    topic = str(value).strip()
    if not topic:
        return ""
    if len(topic) > 80 or any(ch not in NTFY_TOPIC_ALPHABET for ch in topic):
        return ""
    return topic


def resolve_global_ntfy_topic(cfg):
    if isinstance(cfg, dict) and "ntfy_topic" in cfg:
        configured_topic = normalize_ntfy_topic(cfg.get("ntfy_topic"))
        return configured_topic or normalize_ntfy_topic(os.getenv("NTFY_TOPIC"))
    return normalize_ntfy_topic(os.getenv("NTFY_TOPIC"))


def optional_bounded_int(value, current, minimum, maximum):
    number = _optional_int(value)
    if number is None:
        return current
    return max(minimum, min(maximum, number))


def short_mint(mint):
    mint = str(mint or "")
    if len(mint) <= 10:
        return mint or "Token"
    return f"{mint[:4]}...{mint[-4:]}"


def active_token_label():
    return str((ACTIVE_TOKEN_CONFIG or {}).get("name") or short_mint(OUTPUT_MINT)).strip() or short_mint(OUTPUT_MINT)


def active_ntfy_topic():
    token_topic = normalize_ntfy_topic((ACTIVE_TOKEN_CONFIG or {}).get("ntfy_topic"))
    if token_topic:
        return token_topic, "custom"
    global_topic = normalize_ntfy_topic(NTFY_TOPIC)
    if global_topic:
        return global_topic, "inherited"
    return "", "disabled"

def token_label(token_config):
    token_config = token_config or {}
    return str(token_config.get("name") or short_mint(token_config.get("mint"))).strip() or short_mint(token_config.get("mint"))


def token_ntfy_topic(token_config, inherited_topic=None):
    token_topic = normalize_ntfy_topic((token_config or {}).get("ntfy_topic"))
    if token_topic:
        return token_topic, "custom"
    global_topic = normalize_ntfy_topic(NTFY_TOPIC if inherited_topic is None else inherited_topic)
    if global_topic:
        return global_topic, "inherited"
    return "", "disabled"


def base_check_interval(cfg):
    return coerce_int((cfg or {}).get("check_interval", os.getenv("CHECK_INTERVAL")), env_int("CHECK_INTERVAL", 60, minimum=5), minimum=5)


def base_rsi_check_interval(cfg):
    return coerce_int((cfg or {}).get("rsi_check_interval", os.getenv("RSI_CHECK_INTERVAL")), env_int("RSI_CHECK_INTERVAL", 4, minimum=1), minimum=1)


def token_check_interval(token_config, cfg=None):
    return optional_bounded_int((token_config or {}).get("check_interval"), base_check_interval(cfg), 5, 86400)


def token_rsi_check_interval(token_config, cfg=None):
    return optional_bounded_int((token_config or {}).get("rsi_check_interval"), base_rsi_check_interval(cfg), 1, 43200)


def token_alert_reset_minutes(token_config, cfg=None):
    default = coerce_int((cfg or {}).get("alert_reset_minutes", ALERT_RESET_MINUTES), ALERT_RESET_MINUTES, minimum=0)
    return coerce_int((token_config or {}).get("alert_reset_minutes", default), default, minimum=0)


def token_rsi_interval(token_config, cfg=None):
    interval = (token_config or {}).get("rsi_interval") or (cfg or {}).get("rsi_interval") or RSI_INTERVAL or "1s"
    return str(interval).strip() or "1s"


def token_rsi_reset_enabled(token_config, cfg=None):
    default = coerce_bool((cfg or {}).get("rsi_reset_enabled"), RSI_RESET_ENABLED)
    return coerce_bool((token_config or {}).get("rsi_reset_enabled"), default)


def token_rsi_enabled(token_config, cfg=None):
    default = coerce_bool((cfg or {}).get("rsi_enabled"), RSI_ENABLED)
    return coerce_bool((token_config or {}).get("rsi_enabled"), default)


def rsi_disabled_reason(token_config=None, cfg=None):
    if not SOLANATRACKER_FEATURES_ENABLED:
        return "SolanaTracker disabled in settings"
    if not solanatracker_api_key_configured():
        return "SolanaTracker API key is not configured"
    if not token_rsi_enabled(token_config, cfg):
        return "RSI disabled for this token"
    return ""


def token_usd_amount(token_config):
    return coerce_float((token_config or {}).get("usd_amount", USD_AMOUNT), USD_AMOUNT, minimum=0.000001)


def token_input_decimals(token_config):
    return _optional_decimals((token_config or {}).get("input_decimals", INPUT_DECIMALS))


def token_output_decimals(token_config):
    return _optional_decimals((token_config or {}).get("output_decimals", OUTPUT_DECIMALS))


def token_rsi_alerts_raw(token_config):
    return rsi_entries_to_raw((token_config or {}).get("rsi_alerts", []))


def parse_rsi_alert_map(raw_alerts):
    parsed = {}
    if not solanatracker_effective_enabled() or not raw_alerts:
        return parsed
    for entry in str(raw_alerts).split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        try:
            direction, value = entry.split(":", 1)
            direction = direction.strip().lower()
            if direction not in {"above", "below"}:
                continue
            threshold = float(value)
            parsed[f"{direction}:{threshold:.2f}"] = {"triggered": False}
        except Exception:
            continue
    return parsed


def get_enabled_token_configs(cfg):
    tokens = (cfg or {}).get("tokens")
    if isinstance(tokens, list):
        enabled = []
        for token in tokens:
            if not isinstance(token, dict) or not token.get("enabled", True):
                continue
            mint = str(token.get("mint") or "").strip()
            if mint:
                enabled.append(dict(token))
        return enabled
    return [dict(ACTIVE_TOKEN_CONFIG or {"mint": OUTPUT_MINT, "name": short_mint(OUTPUT_MINT), "ntfy_topic": ""})]


def token_state_from_shared(mint):
    state_data = read_json_file(shared_json_path)
    token_states = state_data.get("token_states", {}) if isinstance(state_data, dict) else {}
    token_state = token_states.get(mint, {}) if isinstance(token_states, dict) else {}
    return state_data, token_state if isinstance(token_state, dict) else {}


def token_state_matches_generation(token_state, token_config):
    expected_nonce = str((token_config or {}).get("runtime_nonce") or "")
    persisted_nonce = str((token_state or {}).get("runtime_nonce") or "")
    # Empty nonces are allowed for state created before generation tracking.
    return not expected_nonce or not persisted_nonce or expected_nonce == persisted_nonce


def get_token_runtime(mint, token_config=None):
    runtime_nonce = str((token_config or {}).get("runtime_nonce") or "")
    runtime = TOKEN_RUNTIMES.get(mint)
    if runtime is not None and str(runtime.get("runtime_nonce") or "") == runtime_nonce:
        return runtime

    state_data, token_state = token_state_from_shared(mint)
    if not token_state_matches_generation(token_state, token_config):
        state_data = {}
        token_state = {}
    buy_triggers = token_state.get("last_triggered_buy") or (state_data.get("last_triggered_buy", {}) if mint == OUTPUT_MINT else {})
    sell_triggers = token_state.get("last_triggered_sell") or (state_data.get("last_triggered_sell", {}) if mint == OUTPUT_MINT else {})
    rsi_triggers = token_state.get("last_triggered_rsi") or (state_data.get("last_triggered_rsi", {}) if mint == OUTPUT_MINT else {})

    runtime = {
        "last_buy_alert": load_trigger_times(buy_triggers),
        "last_sell_alert": load_trigger_times(sell_triggers),
        "last_triggered_rsi": dict(rsi_triggers or {}),
        "rsi_state": {},
        "last_rsi_at": parse_iso_to_utc(token_state.get("rsi_last_attempt_at") or token_state.get("rsi_last_fetch_at") or token_state.get("latest_rsi_time")),
        "latest_rsi": token_state.get("latest_rsi"),
        "latest_rsi_time": token_state.get("latest_rsi_time"),
        "rsi_status": token_state.get("rsi_status") or ("waiting" if solanatracker_effective_enabled() else "disabled"),
        "rsi_error": token_state.get("rsi_error"),
        "rsi_last_fetch_at": token_state.get("rsi_last_fetch_at"),
        "rsi_last_attempt_at": token_state.get("rsi_last_attempt_at") or token_state.get("rsi_last_fetch_at"),
        "rsi_last_success_at": token_state.get("rsi_last_success_at") or token_state.get("rsi_last_fetch_at"),
        "rsi_source": dict(token_state.get("rsi_source") or {}) if isinstance(token_state.get("rsi_source"), dict) else {},
        "rsi_refresh_nonce": token_state.get("rsi_refresh_nonce", ""),
        "runtime_nonce": runtime_nonce,
    }
    TOKEN_RUNTIMES[mint] = runtime
    return runtime


def parse_iso_to_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def unpack_rsi_result(result, expected_interval):
    if not isinstance(result, (tuple, list)) or len(result) < 2:
        raise ValueError("Invalid RSI result")
    value = float(result[0])
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("RSI result is outside 0-100")
    rsi_time = str(result[1] or "").strip()
    candidate_time = parse_iso_to_utc(rsi_time)
    if candidate_time is None:
        raise ValueError("RSI result has an invalid candle timestamp")

    metadata = result[2] if len(result) > 2 and isinstance(result[2], dict) else None
    if metadata is None:
        return value, rsi_time, {}

    source_time = parse_iso_to_utc(metadata.get("timestamp") or rsi_time)
    try:
        close = float(metadata.get("close"))
        volume = float(metadata.get("volume"))
    except (TypeError, ValueError):
        raise ValueError("RSI result has invalid candle metadata")
    interval = str(metadata.get("interval") or expected_interval or "").strip()
    if source_time is None or source_time != candidate_time or not math.isfinite(close) or close <= 0 or not math.isfinite(volume) or volume <= 0:
        raise ValueError("RSI result has invalid candle metadata")
    if interval != str(expected_interval or "").strip():
        raise ValueError("RSI result interval does not match the configured interval")

    source = {
        "timestamp": source_time.isoformat(),
        "close": close,
        "volume": volume,
        "interval": interval,
        "active_bars": coerce_int(metadata.get("active_bars"), 0, minimum=0),
        "calculation_version": str(metadata.get("calculation_version") or ""),
    }
    return value, source_time.isoformat(), source


def rsi_sources_match(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_time = parse_iso_to_utc(left.get("timestamp"))
    right_time = parse_iso_to_utc(right.get("timestamp"))
    try:
        left_close = float(left.get("close"))
        right_close = float(right.get("close"))
        left_volume = float(left.get("volume"))
        right_volume = float(right.get("volume"))
    except (TypeError, ValueError):
        return False
    return (
        left_time is not None
        and left_time == right_time
        and left_close == right_close
        and left_volume == right_volume
        and str(left.get("interval") or "") == str(right.get("interval") or "")
    )


def apply_trusted_rsi_result(runtime, result, interval, observed_at=None):
    """Accept a new/changed source candle while holding unchanged trusted RSI."""
    value, rsi_time, source = unpack_rsi_result(result, interval)
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    runtime["rsi_last_attempt_at"] = observed_at
    runtime["rsi_last_fetch_at"] = observed_at

    candidate_time = parse_iso_to_utc(rsi_time)
    previous_time = parse_iso_to_utc(runtime.get("latest_rsi_time"))
    previous_source = runtime.get("rsi_source") if isinstance(runtime.get("rsi_source"), dict) else {}

    if previous_time is not None and candidate_time < previous_time:
        runtime["rsi_status"] = "stale"
        runtime["rsi_error"] = "SolanaTracker returned an older RSI candle; keeping the previous value"
        return "regressed", runtime.get("latest_rsi")

    if source and previous_time is not None and candidate_time == previous_time:
        if rsi_sources_match(previous_source, source):
            runtime["rsi_status"] = "ok"
            runtime["rsi_error"] = None
            runtime["rsi_last_success_at"] = observed_at
            return "unchanged", runtime.get("latest_rsi")
        if not previous_source and runtime.get("latest_rsi") is not None:
            # Safe migration from pre-signature state: establish the source
            # identity without replacing a trusted value on the same candle.
            runtime["rsi_source"] = source
            runtime["rsi_status"] = "ok"
            runtime["rsi_error"] = None
            runtime["rsi_last_success_at"] = observed_at
            return "unchanged", runtime.get("latest_rsi")

    runtime["latest_rsi"] = round(value, 2)
    runtime["latest_rsi_time"] = rsi_time
    runtime["rsi_source"] = source
    runtime["rsi_status"] = "ok"
    runtime["rsi_error"] = None
    runtime["rsi_last_success_at"] = observed_at
    return "accepted", runtime["latest_rsi"]


def clear_runtime_rsi(runtime, status, error=None):
    runtime["last_rsi_at"] = None
    runtime["latest_rsi"] = None
    runtime["latest_rsi_time"] = None
    runtime["rsi_source"] = {}
    runtime["rsi_status"] = status
    runtime["rsi_error"] = error
    runtime["rsi_last_fetch_at"] = None
    runtime["rsi_last_attempt_at"] = None
    runtime["rsi_last_success_at"] = None


def sync_runtime_rsi_state(runtime, raw_alerts):
    current = runtime.get("rsi_state") or {}
    persisted = runtime.get("last_triggered_rsi") or {}
    next_state = parse_rsi_alert_map(raw_alerts)
    for key in next_state:
        next_state[key]["triggered"] = bool(current.get(key, {}).get("triggered") or key in persisted)
    runtime["rsi_state"] = next_state
    runtime["last_triggered_rsi"] = {k: v for k, v in persisted.items() if k in next_state}
    return next_state


def scheduler_last_check_for(mint, token_config=None):
    if token_config is not None:
        runtime_nonce = str((token_config or {}).get("runtime_nonce") or "")
        previous_nonce = SCHEDULER_TOKEN_NONCES.get(mint)
        if previous_nonce is not None and previous_nonce != runtime_nonce:
            SCHEDULER_LAST_CHECK.pop(mint, None)
        SCHEDULER_TOKEN_NONCES[mint] = runtime_nonce
    if mint in SCHEDULER_LAST_CHECK:
        return SCHEDULER_LAST_CHECK[mint]
    _state_data, token_state = token_state_from_shared(mint)
    if not token_state_matches_generation(token_state, token_config):
        return 0.0
    checked = parse_iso_to_utc(token_state.get("last_checked_at") or token_state.get("timestamp"))
    if checked:
        SCHEDULER_LAST_CHECK[mint] = checked.timestamp()
        return SCHEDULER_LAST_CHECK[mint]
    return 0.0

INPUT_DECIMALS = _optional_decimals(os.getenv("INPUT_DECIMALS"))
OUTPUT_DECIMALS = _optional_decimals(os.getenv("OUTPUT_DECIMALS"))
_DECIMALS_CACHE = {}

LATEST_RSI = None
LATEST_RSI_TIME = None
LATEST_RSI_STATUS = "waiting" if solanatracker_effective_enabled() else "disabled"
LATEST_RSI_ERROR = None
LATEST_RSI_LAST_FETCH_AT = None
LATEST_RSI_SOURCE = {}
LATEST_RSI_LAST_ATTEMPT_AT = None
LATEST_RSI_LAST_SUCCESS_AT = None

def sync_active_rsi_globals(runtime):
    """Mirror the active token's persisted runtime into legacy UI globals."""
    global _last_rsi_at, LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR
    global LATEST_RSI_LAST_FETCH_AT, LATEST_RSI_SOURCE, LATEST_RSI_LAST_ATTEMPT_AT, LATEST_RSI_LAST_SUCCESS_AT

    _last_rsi_at = runtime.get("last_rsi_at")
    LATEST_RSI = runtime.get("latest_rsi")
    LATEST_RSI_TIME = runtime.get("latest_rsi_time")
    LATEST_RSI_STATUS = runtime.get("rsi_status") or "waiting"
    LATEST_RSI_ERROR = runtime.get("rsi_error")
    LATEST_RSI_LAST_FETCH_AT = runtime.get("rsi_last_fetch_at")
    LATEST_RSI_SOURCE = dict(runtime.get("rsi_source") or {})
    LATEST_RSI_LAST_ATTEMPT_AT = runtime.get("rsi_last_attempt_at")
    LATEST_RSI_LAST_SUCCESS_AT = runtime.get("rsi_last_success_at")


def seed_active_rsi_runtime_from_globals(runtime):
    """Compatibility bridge for an already-running pre-runtime active state."""
    if runtime.get("latest_rsi") is not None or LATEST_RSI is None:
        return
    runtime.update({
        "last_rsi_at": _last_rsi_at,
        "latest_rsi": LATEST_RSI,
        "latest_rsi_time": LATEST_RSI_TIME,
        "rsi_status": LATEST_RSI_STATUS,
        "rsi_error": LATEST_RSI_ERROR,
        "rsi_last_fetch_at": LATEST_RSI_LAST_FETCH_AT,
        "rsi_source": dict(LATEST_RSI_SOURCE or {}),
        "rsi_last_attempt_at": LATEST_RSI_LAST_ATTEMPT_AT,
        "rsi_last_success_at": LATEST_RSI_LAST_SUCCESS_AT,
    })


print("Starting script, checking env vars...", flush=True)
print(f"INPUT_MINT: {INPUT_MINT}", flush=True)
print(f"OUTPUT_MINT: {OUTPUT_MINT}", flush=True)


def read_json_file(path):
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def is_unset_mint(mint):
    value = str(mint or "").strip()
    return not value or value.startswith("<")


def hydrate_output_mint_from_config():
    global OUTPUT_MINT

    if not is_unset_mint(OUTPUT_MINT):
        return

    cfg = read_json_file(config_json_path)
    tokens = cfg.get("tokens") if isinstance(cfg, dict) else None
    if not isinstance(tokens, list):
        return

    active_mint = str(cfg.get("active_token_mint") or "").strip()
    for token in tokens:
        mint = str(token.get("mint") or "").strip() if isinstance(token, dict) else ""
        if mint and mint == active_mint:
            OUTPUT_MINT = mint
            print(f"Using persisted active token from config: {OUTPUT_MINT}", flush=True)
            return

    for token in tokens:
        mint = str(token.get("mint") or "").strip() if isinstance(token, dict) else ""
        enabled = token.get("enabled", True) if isinstance(token, dict) else False
        if mint and enabled:
            OUTPUT_MINT = mint
            print(f"Using first persisted token from config: {OUTPUT_MINT}", flush=True)
            return


hydrate_output_mint_from_config()

if not INPUT_MINT or is_unset_mint(OUTPUT_MINT):
    print("Missing required INPUT_MINT or OUTPUT_MINT. Exiting.", flush=True)
    exit(1)


def atomic_write_json(path, data):
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)

@contextmanager
def json_file_lock(path):
    """Best-effort cross-process lock for shared JSON read/merge/write cycles."""
    lock_path = f"{path}.lock"
    if os.name != "posix":
        yield
        return

    try:
        import fcntl

        directory = os.path.dirname(lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception:
        yield


def __load_persisted_rsi():
    return read_json_file(shared_json_path).get("last_triggered_rsi", {})


def normalize_price_alerts(values):
    if isinstance(values, str):
        raw_values = values.split(",")
    else:
        raw_values = values or []

    alerts = set()
    for raw in raw_values:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            alerts.add(value)
    return sorted(alerts)


def parse_env_alerts(env_value):
    return normalize_price_alerts(env_value)


def load_trigger_times(raw_times):
    local_tz = datetime.now().astimezone().tzinfo
    parsed = {}
    for key, value in (raw_times or {}).items():
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        parsed[key] = dt
    return parsed


def parse_rsi_alerts():
    global RSI_STATE
    RSI_STATE.clear()
    if not solanatracker_effective_enabled() or not RSI_ALERTS_RAW:
        return
    for entry in RSI_ALERTS_RAW.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        try:
            direction, value = entry.split(":", 1)
            direction = direction.strip().lower()
            if direction not in {"above", "below"}:
                continue
            threshold = float(value)
            key = f"{direction}:{threshold:.2f}"
            RSI_STATE[key] = {"triggered": False}
        except:
            continue


parse_rsi_alerts()

# On startup, sync in-memory RSI_STATE.triggered from shared JSON.
try:
    with open(shared_json_path) as sf:
        shared = json.load(sf)
    persisted = shared.get("last_triggered_rsi", {})
    for k in RSI_STATE:
        triggered_at = persisted.get(k)
        RSI_STATE[k]["triggered"] = triggered_at is not None
        if triggered_at is not None:
            RSI_STATE[k]["triggered_at"] = triggered_at
except Exception:
    pass

def rsi_entries_to_raw(entries):
    if not entries:
        return ""
    if isinstance(entries, str):
        return entries
    return ",".join(str(entry) for entry in entries)


def get_active_token_config(cfg):
    tokens = cfg.get("tokens")
    if not isinstance(tokens, list):
        return None

    active_mint = str(cfg.get("active_token_mint") or "").strip()
    for token in tokens:
        if isinstance(token, dict) and str(token.get("mint") or "").strip() == active_mint:
            return token

    for token in tokens:
        if isinstance(token, dict) and token.get("enabled", True) and str(token.get("mint") or "").strip():
            return token
    return None


def reset_active_token_runtime(new_mint):
    global OUTPUT_MINT, _last_rsi_at, LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR, LATEST_RSI_LAST_FETCH_AT, LATEST_RSI_SOURCE, LATEST_RSI_LAST_ATTEMPT_AT, LATEST_RSI_LAST_SUCCESS_AT, TOKEN_CHANGED_SINCE_LAST_WRITE

    if not new_mint or new_mint == OUTPUT_MINT:
        return False

    previous_mint = OUTPUT_MINT
    print(f"Active token changed: {previous_mint} -> {new_mint}", flush=True)
    TOKEN_RUNTIMES.pop(previous_mint, None)
    TOKEN_RUNTIMES.pop(new_mint, None)
    OUTPUT_MINT = new_mint
    last_buy_alert.clear()
    last_sell_alert.clear()
    RSI_STATE.clear()
    _last_rsi_at = None
    LATEST_RSI = None
    LATEST_RSI_TIME = None
    LATEST_RSI_STATUS = "waiting" if solanatracker_effective_enabled() else "disabled"
    LATEST_RSI_ERROR = None
    LATEST_RSI_LAST_FETCH_AT = None
    LATEST_RSI_SOURCE = {}
    LATEST_RSI_LAST_ATTEMPT_AT = None
    LATEST_RSI_LAST_SUCCESS_AT = None
    TOKEN_CHANGED_SINCE_LAST_WRITE = True
    return True


def load_dynamic_config():
    global USD_AMOUNT, BUY_ALERTS, SELL_ALERTS, ALERT_RESET_MINUTES, CHECK_INTERVAL
    global RSI_ALERTS_RAW, RSI_INTERVAL, RSI_RESET_ENABLED, RSI_CHECK_INTERVAL, RSI_ENABLED
    global SOLANATRACKER_RATE_LIMIT_MODE, SOLANATRACKER_REQUESTS_PER_SECOND, SOLANATRACKER_FEATURES_ENABLED, INPUT_DECIMALS, OUTPUT_DECIMALS, OUTPUT_MINT, ACTIVE_TOKEN_CONFIG, NTFY_TOPIC
    global ACTIVE_RSI_REFRESH_NONCE, _last_rsi_at
    global LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR, LATEST_RSI_LAST_FETCH_AT, LATEST_RSI_SOURCE, LATEST_RSI_LAST_ATTEMPT_AT, LATEST_RSI_LAST_SUCCESS_AT

    token_changed = False

    if os.path.exists(config_json_path):
        try:
            cfg = read_json_file(config_json_path)

            USD_AMOUNT = coerce_float(cfg.get("usd_amount", USD_AMOUNT), USD_AMOUNT, minimum=0.000001)
            BUY_ALERTS = normalize_price_alerts(cfg.get("buy_alerts", BUY_ALERTS))
            SELL_ALERTS = normalize_price_alerts(cfg.get("sell_alerts", SELL_ALERTS))
            ALERT_RESET_MINUTES = coerce_int(cfg.get("alert_reset_minutes", ALERT_RESET_MINUTES), ALERT_RESET_MINUTES, minimum=0)
            CHECK_INTERVAL = coerce_int(cfg.get("check_interval", CHECK_INTERVAL), CHECK_INTERVAL, minimum=5)
            RSI_CHECK_INTERVAL = coerce_int(cfg.get("rsi_check_interval", RSI_CHECK_INTERVAL), RSI_CHECK_INTERVAL, minimum=1)
            SOLANATRACKER_FEATURES_ENABLED = coerce_bool(cfg.get("solanatracker_features_enabled", SOLANATRACKER_FEATURES_ENABLED), SOLANATRACKER_FEATURES_ENABLED)
            SOLANATRACKER_RATE_LIMIT_MODE = normalize_rate_limit_mode(cfg.get("solanatracker_rate_limit_mode", SOLANATRACKER_RATE_LIMIT_MODE), cfg.get("solanatracker_requests_per_second", SOLANATRACKER_REQUESTS_PER_SECOND))
            SOLANATRACKER_REQUESTS_PER_SECOND = coerce_float(
                cfg.get("solanatracker_requests_per_second", SOLANATRACKER_REQUESTS_PER_SECOND),
                SOLANATRACKER_REQUESTS_PER_SECOND,
                minimum=0.1,
                maximum=50.0,
            )
            apply_solanatracker_rate_limit()
            NTFY_TOPIC = resolve_global_ntfy_topic(cfg)
            INPUT_DECIMALS = _optional_decimals(cfg.get("input_decimals", INPUT_DECIMALS))
            OUTPUT_DECIMALS = _optional_decimals(cfg.get("output_decimals", OUTPUT_DECIMALS))

            active_token = get_active_token_config(cfg)
            if active_token:
                ACTIVE_TOKEN_CONFIG = dict(active_token)
                new_output_mint = str(active_token.get("mint") or "").strip()
                token_changed = reset_active_token_runtime(new_output_mint)
                USD_AMOUNT = coerce_float(active_token.get("usd_amount", USD_AMOUNT), USD_AMOUNT, minimum=0.000001)
                BUY_ALERTS = normalize_price_alerts(active_token.get("buy_alerts", BUY_ALERTS))
                SELL_ALERTS = normalize_price_alerts(active_token.get("sell_alerts", SELL_ALERTS))
                ALERT_RESET_MINUTES = token_alert_reset_minutes(active_token, cfg)
                CHECK_INTERVAL = optional_bounded_int(active_token.get("check_interval"), CHECK_INTERVAL, 5, 86400)
                RSI_CHECK_INTERVAL = optional_bounded_int(active_token.get("rsi_check_interval"), RSI_CHECK_INTERVAL, 1, 43200)
                INPUT_DECIMALS = _optional_decimals(active_token.get("input_decimals", INPUT_DECIMALS))
                OUTPUT_DECIMALS = _optional_decimals(active_token.get("output_decimals", OUTPUT_DECIMALS))
                if "rsi_alerts" in active_token:
                    RSI_ALERTS_RAW = rsi_entries_to_raw(active_token.get("rsi_alerts"))
            else:
                ACTIVE_TOKEN_CONFIG = {"mint": OUTPUT_MINT, "name": short_mint(OUTPUT_MINT), "ntfy_topic": ""} if OUTPUT_MINT else {}
                if "rsi_alerts" in cfg:
                    RSI_ALERTS_RAW = rsi_entries_to_raw(cfg.get("rsi_alerts"))
            RSI_INTERVAL = cfg.get("rsi_interval", RSI_INTERVAL)
            RSI_RESET_ENABLED = coerce_bool(cfg.get("rsi_reset_enabled", RSI_RESET_ENABLED), RSI_RESET_ENABLED)
            RSI_ENABLED = coerce_bool(cfg.get("rsi_enabled", RSI_ENABLED), RSI_ENABLED)
            if active_token:
                RSI_INTERVAL = token_rsi_interval(active_token, cfg)
                RSI_RESET_ENABLED = token_rsi_reset_enabled(active_token, cfg)
                RSI_ENABLED = token_rsi_enabled(active_token, cfg)
            refresh_nonce = str((active_token or {}).get("rsi_refresh_nonce") or "")
            if refresh_nonce != ACTIVE_RSI_REFRESH_NONCE:
                ACTIVE_RSI_REFRESH_NONCE = refresh_nonce
                _last_rsi_at = None
                LATEST_RSI = None
                LATEST_RSI_TIME = None
                LATEST_RSI_STATUS = "waiting" if not rsi_disabled_reason(active_token, cfg) else "disabled"
                LATEST_RSI_ERROR = None if LATEST_RSI_STATUS == "waiting" else rsi_disabled_reason(active_token, cfg)
                LATEST_RSI_LAST_FETCH_AT = None
                LATEST_RSI_SOURCE = {}
                LATEST_RSI_LAST_ATTEMPT_AT = None
                LATEST_RSI_LAST_SUCCESS_AT = None
            parse_rsi_alerts()

            state_data = read_json_file(shared_json_path)
            if token_changed:
                token_states = state_data.get("token_states", {}) if isinstance(state_data, dict) else {}
                token_state = token_states.get(OUTPUT_MINT, {}) if isinstance(token_states, dict) else {}
                persisted_rsi = token_state.get("last_triggered_rsi", {}) if isinstance(token_state, dict) else {}
            else:
                persisted_rsi = state_data.get("last_triggered_rsi", {}) if isinstance(state_data, dict) else {}
            for k in RSI_STATE:
                triggered_at = persisted_rsi.get(k)
                RSI_STATE[k]["triggered"] = triggered_at is not None
                if triggered_at is not None:
                    RSI_STATE[k]["triggered_at"] = triggered_at
                else:
                    RSI_STATE[k].pop("triggered_at", None)

        except Exception as e:
            print(f"Failed to load config.json: {e}", flush=True)
    else:
        print("No config.json found; using ENV defaults", flush=True)
        BUY_ALERTS = parse_env_alerts(os.getenv("BUY_ALERTS", ""))
        SELL_ALERTS = parse_env_alerts(os.getenv("SELL_ALERTS", ""))
        ALERT_RESET_MINUTES = env_int("ALERT_RESET_MINUTES", ALERT_RESET_MINUTES, minimum=0)
        CHECK_INTERVAL = env_int("CHECK_INTERVAL", CHECK_INTERVAL, minimum=5)
        RSI_CHECK_INTERVAL = env_int("RSI_CHECK_INTERVAL", RSI_CHECK_INTERVAL, minimum=1)
        RSI_ENABLED = coerce_bool(os.getenv("RSI_ENABLED", RSI_ENABLED), RSI_ENABLED)
        SOLANATRACKER_FEATURES_ENABLED = coerce_bool(os.getenv("SOLANATRACKER_ENABLED", os.getenv("SOLANATRACKER_FEATURES_ENABLED", SOLANATRACKER_FEATURES_ENABLED)), SOLANATRACKER_FEATURES_ENABLED)
        SOLANATRACKER_RATE_LIMIT_MODE = normalize_rate_limit_mode(os.getenv("SOLANATRACKER_RATE_LIMIT_MODE", SOLANATRACKER_RATE_LIMIT_MODE), os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND", SOLANATRACKER_REQUESTS_PER_SECOND))
        SOLANATRACKER_REQUESTS_PER_SECOND = env_float("SOLANATRACKER_REQUESTS_PER_SECOND", SOLANATRACKER_REQUESTS_PER_SECOND, minimum=0.1, maximum=50.0)
        apply_solanatracker_rate_limit()
        NTFY_TOPIC = resolve_global_ntfy_topic({})
        ACTIVE_TOKEN_CONFIG = {"mint": OUTPUT_MINT, "name": short_mint(OUTPUT_MINT), "ntfy_topic": ""} if OUTPUT_MINT else {}
    if os.path.exists(shared_json_path):
        state_data = read_json_file(shared_json_path)
        if not state_data:
            return

        if token_changed:
            token_states = state_data.get("token_states", {}) if isinstance(state_data, dict) else {}
            token_state = token_states.get(OUTPUT_MINT, {}) if isinstance(token_states, dict) else {}
            last_buy_alert.clear()
            last_buy_alert.update(load_trigger_times(token_state.get("last_triggered_buy", {})))
            last_sell_alert.clear()
            last_sell_alert.update(load_trigger_times(token_state.get("last_triggered_sell", {})))
        else:
            last_buy_alert.clear()
            last_buy_alert.update(load_trigger_times(state_data.get("last_triggered_buy", {})))

            last_sell_alert.clear()
            last_sell_alert.update(load_trigger_times(state_data.get("last_triggered_sell", {})))

    valid_buy_keys = {f"{float(x):.8f}" for x in BUY_ALERTS}
    for k in list(last_buy_alert):
        if k not in valid_buy_keys:
            last_buy_alert.pop(k)
    valid_sell_keys = {f"{float(x):.8f}" for x in SELL_ALERTS}
    for k in list(last_sell_alert):
        if k not in valid_sell_keys:
            last_sell_alert.pop(k)


def resolve_token_decimals(mint, configured_decimals=None):
    if configured_decimals is not None:
        return int(configured_decimals)
    if mint == USDC_MINT:
        return 6
    if mint in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[mint]

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    try:
        resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=10)
        resp.raise_for_status()
        info = resp.json().get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {})
        decimals = int(info.get("decimals"))
        _DECIMALS_CACHE[mint] = decimals
        return decimals
    except Exception as e:
        print(f"Could not resolve decimals for {mint}; falling back to 6: {e}", flush=True)
        _DECIMALS_CACHE[mint] = 6
        return 6


def amount_to_atomic(amount, decimals):
    scale = Decimal(10) ** int(decimals)
    return int((Decimal(str(amount)) * scale).to_integral_value(rounding=ROUND_DOWN))


def atomic_to_amount(raw_amount, decimals):
    return int(raw_amount) / (10 ** int(decimals))


def send_alert(title, message, token_config=None, inherited_topic=None):
    token_config = token_config or ACTIVE_TOKEN_CONFIG
    topic, source = token_ntfy_topic(token_config, inherited_topic=inherited_topic)
    if not topic:
        print("Alert skipped: no ntfy topic configured", flush=True)
        return False
    try:
        mint = str((token_config or {}).get("mint") or OUTPUT_MINT)
        label = token_label(token_config)
        scoped_title = f"{title} - {label}"
        scoped_message = f"{message}\nToken: {label} ({mint})\nTopic source: {source}"
        url = f"{NTFY_SERVER.rstrip('/')}/{topic}"
        response = requests.post(
            url,
            data=scoped_message.encode("utf-8"),
            headers={"Title": scoped_title, "Content-Type": "text/plain; charset=utf-8"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Failed to send alert: {e}", flush=True)

        return False

def notify_backend_trigger(side: str, price: float):
    try:
        requests.post("http://127.0.0.1:8000/api/trigger", json={
            "side": side,
            "price": round(price, 8),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, timeout=2)
    except Exception as e:
        print(f"Failed to notify backend of {side} trigger: {e}", flush=True)

def notify_backend_rsi_trigger(key: str, timestamp: str):
    try:
        response = requests.post(
            "http://127.0.0.1:8000/api/rsi/trigger",
            json={"key": key, "timestamp": timestamp},
            timeout=2,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Failed to notify backend of RSI trigger: {e}", flush=True)
        return False

def get_out_amount_raw(input_mint, output_mint, amount_lamports, cache_result=True, quote_capture=None):
    """
    Uses Jupiter Swap V2 order in quote-only mode and returns raw outAmount.
    The caller is responsible for converting raw token units with the output mint decimals.
    Price checks can stage quotes in quote_capture so unconfirmed samples never enter shared caches.
    """
    try:
        quote = get_quote(input_mint, output_mint, amount_lamports)
        key = (str(input_mint), str(output_mint), int(amount_lamports))
        cache_entry = {
            "quote": quote,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if cache_result:
            QUOTE_CACHE[key] = cache_entry
        if isinstance(quote_capture, dict):
            quote_capture[key] = cache_entry
        return int(quote.get("outAmount", "0"))
    except JupiterQuoteError as e:
        print(f"Jupiter quote failed after retries: {e}", flush=True)
        return None


def should_alert(alert_dict, key, reset_minutes=None):
    """
    Decide whether we should fire an alert for key, and return
    (allow: bool, timestamp_to_set: datetime or None).

    - If ALERT_RESET_MINUTES == 0: only allow on first encounter (when key not in alert_dict).
      Once triggered, it will remain blocked until you call reset (which removes alert_dict[key]).
    - If ALERT_RESET_MINUTES > 0: allow when there's no timestamp or the cooldown has expired.
    """
    now_utc = datetime.now(timezone.utc)
    minutes = ALERT_RESET_MINUTES if reset_minutes is None else coerce_int(reset_minutes, ALERT_RESET_MINUTES, minimum=0)
    last_time = alert_dict.get(key)

    # Zero-reset mode: fire once, then block until manual reset
    if minutes == 0:
        if last_time is None:
            return True, now_utc    # first trigger
        else:
            return False, None      # already triggered, stay off

    # From here on reset minutes > 0

    # Normalize older, naive timestamps to UTC
    if last_time and last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=timezone.utc)

    # No previous trigger or cooldown expired: allow and clear old timestamp
    if not last_time or (now_utc - last_time) >= timedelta(minutes=minutes):
        if last_time:
            alert_dict.pop(key, None)
        return True, now_utc

    # Still in cooldown
    return False, None




def _history_price(value):
    try:
        if value is None:
            return None
        number = float(value)
        if number <= 0 or number != number or number in (float("inf"), float("-inf")):
            return None
        return round(number, 8)
    except (TypeError, ValueError):
        return None


def prune_history_points(points, now=None):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=PRICE_HISTORY_RETENTION_HOURS)
    cleaned = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        timestamp = parse_iso_to_utc(point.get("timestamp") or point.get("time"))
        if not timestamp or timestamp < cutoff:
            continue
        buy_price = _history_price(point.get("buy_price", point.get("buy")))
        sell_price = _history_price(point.get("sell_price", point.get("sell")))
        if buy_price is None and sell_price is None:
            continue
        cleaned.append({
            "timestamp": timestamp.isoformat(),
            "buy_price": buy_price,
            "sell_price": sell_price,
        })
    cleaned.sort(key=lambda point: point["timestamp"])
    return cleaned[-PRICE_HISTORY_MAX_POINTS_PER_TOKEN:]


def append_token_price_history(existing, mint, timestamp, price_buy, price_sell):
    mint = str(mint or "").strip()
    if not mint:
        return []
    histories = existing.get("token_price_history", {}) if isinstance(existing, dict) else {}
    if not isinstance(histories, dict):
        histories = {}
    points = prune_history_points(histories.get(mint, []))
    buy_price = _history_price(price_buy)
    sell_price = _history_price(price_sell)
    if buy_price is not None or sell_price is not None:
        point_time = parse_iso_to_utc(timestamp) or datetime.now(timezone.utc)
        point = {
            "timestamp": point_time.isoformat(),
            "buy_price": buy_price,
            "sell_price": sell_price,
        }
        if points and points[-1].get("timestamp") == point["timestamp"]:
            points[-1] = point
        else:
            points.append(point)
    points = prune_history_points(points)
    histories[mint] = points
    existing["token_price_history"] = histories
    return points


def get_token_price_history(source, mint):
    mint = str(mint or "").strip()
    histories = source.get("token_price_history", {}) if isinstance(source, dict) else {}
    if isinstance(histories, dict) and mint in histories:
        return prune_history_points(histories.get(mint, []))
    return prune_history_points(source.get("latest_prices", [])) if isinstance(source, dict) else []



def valid_price_sample(value):
    try:
        number = float(value)
        return number if number > 0 and number == number and number not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError, OverflowError):
        return None


def price_confirmation_signature(token_config):
    token_config = token_config or {}
    return (
        str(token_config.get("mint") or "").strip(),
        str(token_config.get("runtime_nonce") or ""),
        round(token_usd_amount(token_config), 12),
        token_input_decimals(token_config),
        token_output_decimals(token_config),
    )


def recent_price_baseline(mint):
    state_data, _token_state = token_state_from_shared(mint)
    histories = state_data.get("token_price_history", {}) if isinstance(state_data, dict) else {}
    if isinstance(histories, dict) and mint in histories:
        candidate_points = histories.get(mint, [])
    else:
        legacy_active_mint = str(state_data.get("active_token_mint") or "") if isinstance(state_data, dict) else ""
        use_legacy_history = legacy_active_mint == mint or (not legacy_active_mint and mint == OUTPUT_MINT)
        candidate_points = state_data.get("latest_prices", []) if use_legacy_history else []
    if not isinstance(candidate_points, list):
        candidate_points = []
    recent_candidates = candidate_points[-(PRICE_MOVE_CONFIRMATION_BASELINE_POINTS * 3):]
    points = prune_history_points(recent_candidates)[-PRICE_MOVE_CONFIRMATION_BASELINE_POINTS:]
    baseline = {}
    for side, field in (("buy", "buy_price"), ("sell", "sell_price")):
        values = [valid_price_sample(point.get(field)) for point in points if isinstance(point, dict)]
        values = [value for value in values if value is not None]
        if len(values) >= PRICE_MOVE_CONFIRMATION_MIN_BASELINE_POINTS:
            baseline[side] = median(values)
    return baseline


def price_change_percent(value, baseline):
    value = valid_price_sample(value)
    baseline = valid_price_sample(baseline)
    if value is None or baseline is None:
        return None
    return abs(value - baseline) / baseline * 100.0


def price_sample_is_extreme(sample, baseline):
    for side in ("buy", "sell"):
        change = price_change_percent(sample.get(side), baseline.get(side))
        if change is not None and change >= PRICE_MOVE_CONFIRMATION_THRESHOLD_PERCENT:
            return True
    return False


def price_samples_are_close(first, second):
    compared = 0
    for side in ("buy", "sell"):
        first_value = valid_price_sample(first.get(side))
        second_value = valid_price_sample(second.get(side))
        if first_value is None or second_value is None:
            continue
        compared += 1
        if abs(first_value - second_value) / first_value * 100.0 > PRICE_MOVE_CONFIRMATION_TOLERANCE_PERCENT:
            return False
    return compared > 0


def assess_price_sample(token_config, price_buy, price_sell, now=None):
    """Quarantine a large move until a bounded, rate-limited follow-up confirms it."""
    mint = str((token_config or {}).get("mint") or "").strip()
    if not mint or not PRICE_MOVE_CONFIRMATION_ENABLED:
        if mint:
            PRICE_CONFIRMATIONS.pop(mint, None)
        return {"status": "accepted", "reason": "disabled"}

    now = time.time() if now is None else float(now)
    signature = price_confirmation_signature(token_config)
    pending = PRICE_CONFIRMATIONS.get(mint)
    if pending and pending.get("signature") != signature:
        PRICE_CONFIRMATIONS.pop(mint, None)
        SCHEDULER_LAST_CHECK.pop(mint, None)
        pending = None

    sample = {
        "buy": valid_price_sample(price_buy),
        "sell": valid_price_sample(price_sell),
        "sampled_at": now,
    }

    if pending:
        pending["attempts"] = int(pending.get("attempts", 1)) + 1
        baseline = pending.get("baseline") or {}
        has_price = sample["buy"] is not None or sample["sell"] is not None
        if has_price and not price_sample_is_extreme(sample, baseline):
            PRICE_CONFIRMATIONS.pop(mint, None)
            return {"status": "accepted", "reason": "recovered"}
        if has_price and any(price_samples_are_close(previous, sample) for previous in pending.get("samples", [])):
            PRICE_CONFIRMATIONS.pop(mint, None)
            return {"status": "accepted", "reason": "confirmed"}
        if has_price:
            pending.setdefault("samples", []).append(sample)
        if pending["attempts"] >= PRICE_MOVE_CONFIRMATION_MAX_SAMPLES:
            PRICE_CONFIRMATIONS.pop(mint, None)
            return {
                "status": "rejected",
                "reason": "ambiguous",
                "message": "Price move could not be confirmed; keeping the last trusted price",
            }
        pending["due_at"] = now + PRICE_MOVE_CONFIRMATION_DELAY_SECONDS
        pending["other_token_needed"] = True
        return {
            "status": "pending",
            "reason": "retry",
            "due_at": pending["due_at"],
            "message": "Verifying an unusual Jupiter price move",
        }

    baseline = recent_price_baseline(mint)
    if not baseline or not price_sample_is_extreme(sample, baseline):
        return {"status": "accepted", "reason": "normal"}

    due_at = now + PRICE_MOVE_CONFIRMATION_DELAY_SECONDS
    PRICE_CONFIRMATIONS[mint] = {
        "signature": signature,
        "baseline": baseline,
        "samples": [sample],
        "attempts": 1,
        "due_at": due_at,
        "other_token_needed": True,
    }
    return {
        "status": "pending",
        "reason": "unusual_move",
        "due_at": due_at,
        "message": "Verifying an unusual Jupiter price move",
    }


def valid_price_confirmation(token_config):
    mint = str((token_config or {}).get("mint") or "").strip()
    pending = PRICE_CONFIRMATIONS.get(mint)
    if pending and pending.get("signature") != price_confirmation_signature(token_config):
        PRICE_CONFIRMATIONS.pop(mint, None)
        SCHEDULER_LAST_CHECK.pop(mint, None)
        return None
    return pending


def note_scheduler_token_processed(mint):
    for pending_mint, pending in PRICE_CONFIRMATIONS.items():
        if pending_mint != mint:
            pending["other_token_needed"] = False

def merge_active_rsi_triggers(persisted_triggers):
    merged = dict(persisted_triggers) if isinstance(persisted_triggers, dict) else {}
    for key, info in RSI_STATE.items():
        if not isinstance(info, dict):
            continue
        if info.get("triggered"):
            triggered_at = info.get("triggered_at") or merged.get(key)
            if triggered_at:
                merged[key] = triggered_at
        else:
            merged.pop(key, None)
    return merged


def write_status_json(
    price_buy,
    price_sell,
    token_received,
    usdc_returned,
    record_history=True,
    check_completed=True,
    error=None,
):
    global TOKEN_CHANGED_SINCE_LAST_WRITE

    try:
        with json_file_lock(shared_json_path):
            existing = read_json_file(shared_json_path)
            timestamp = datetime.now(timezone.utc).isoformat()
            ntfy_topic, ntfy_source = active_ntfy_topic()
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            active_token_state = token_states.get(OUTPUT_MINT, {}) if OUTPUT_MINT else {}
            if not isinstance(active_token_state, dict):
                active_token_state = {}
            runtime_nonce = str((ACTIVE_TOKEN_CONFIG or {}).get("runtime_nonce") or "")
            persisted_nonce = str(active_token_state.get("runtime_nonce") or "")
            if runtime_nonce and persisted_nonce and runtime_nonce != persisted_nonce:
                print(f"Skipped stale active token state write for {OUTPUT_MINT}", flush=True)
                return None
            if check_completed and record_history:
                active_history = append_token_price_history(existing, OUTPUT_MINT, timestamp, price_buy, price_sell)
            else:
                active_history = get_token_price_history(existing, OUTPUT_MINT)
            active_rsi_triggers = active_token_state.get("last_triggered_rsi", {}) if TOKEN_CHANGED_SINCE_LAST_WRITE else existing.get("last_triggered_rsi", {})
            active_rsi_triggers = merge_active_rsi_triggers(active_rsi_triggers)
            if OUTPUT_MINT:
                next_token_state = {
                    **active_token_state,
                    "runtime_nonce": runtime_nonce or persisted_nonce,
                    "latest_rsi": LATEST_RSI,
                    "latest_rsi_time": LATEST_RSI_TIME,
                    "rsi_status": LATEST_RSI_STATUS,
                    "rsi_error": LATEST_RSI_ERROR,
                    "rsi_enabled": RSI_ENABLED,
                    "rsi_refresh_nonce": str((ACTIVE_TOKEN_CONFIG or {}).get("rsi_refresh_nonce") or ""),
                    "ntfy_topic": normalize_ntfy_topic((ACTIVE_TOKEN_CONFIG or {}).get("ntfy_topic")),
                    "ntfy_effective_topic": ntfy_topic,
                    "ntfy_topic_source": ntfy_source,
                    "check_interval": CHECK_INTERVAL,
                    "rsi_check_interval": RSI_CHECK_INTERVAL,
                    "last_triggered_buy": {k: v.isoformat() for k, v in last_buy_alert.items()},
                    "last_triggered_sell": {k: v.isoformat() for k, v in last_sell_alert.items()},
                    "last_triggered_rsi": active_rsi_triggers,
                    "rsi_last_fetch_at": LATEST_RSI_LAST_FETCH_AT,
                    "rsi_last_attempt_at": LATEST_RSI_LAST_ATTEMPT_AT,
                    "rsi_last_success_at": LATEST_RSI_LAST_SUCCESS_AT,
                    "rsi_source": dict(LATEST_RSI_SOURCE or {}),
                }
                if check_completed:
                    next_token_state.update({
                        "timestamp": timestamp,
                        "last_checked_at": timestamp,
                        "next_check_at": datetime.fromtimestamp(time.time() + CHECK_INTERVAL, timezone.utc).isoformat(),
                        "buy_price": round(price_buy, 8) if price_buy else None,
                        "sell_price": round(price_sell, 8) if price_sell else None,
                        "token_received": round(token_received, 8) if token_received else None,
                        "usdc_returned": round(usdc_returned, 8) if usdc_returned else None,
                        "error": error,
                        "price_status": "error" if error else "ok",
                        "price_message": error,
                        "price_verification_due_at": None,
                    })
                token_states[OUTPUT_MINT] = next_token_state
            next_existing = {
                "timestamp": timestamp,
                "active_token_mint": OUTPUT_MINT,
                "token_states": token_states,
                "token_price_history": existing.get("token_price_history", {}),
                "latest_prices": active_history,
                "usd_amount": USD_AMOUNT,
                "buy_alerts": BUY_ALERTS,
                "sell_alerts": SELL_ALERTS,
                "last_triggered_buy": {k: v.isoformat() for k, v in last_buy_alert.items()},
                "last_triggered_sell": {k: v.isoformat() for k, v in last_sell_alert.items()},
                "last_triggered_rsi": active_rsi_triggers,
                "alert_reset_minutes": ALERT_RESET_MINUTES,
                "check_interval": CHECK_INTERVAL,
                "rsi_check_interval": RSI_CHECK_INTERVAL,
                "solanatracker_rate_limit_mode": SOLANATRACKER_RATE_LIMIT_MODE,
                "solanatracker_requests_per_second": SOLANATRACKER_REQUESTS_PER_SECOND,
                "solanatracker_features_enabled": SOLANATRACKER_FEATURES_ENABLED,
                "solanatracker_effective_requests_per_second": effective_rate_limit_rps(SOLANATRACKER_RATE_LIMIT_MODE, SOLANATRACKER_REQUESTS_PER_SECOND),
                "input_decimals": resolve_token_decimals(INPUT_MINT, INPUT_DECIMALS),
                "output_decimals": resolve_token_decimals(OUTPUT_MINT, OUTPUT_DECIMALS),
                "latest_rsi": LATEST_RSI,
                "latest_rsi_time": LATEST_RSI_TIME,
                "rsi_status": LATEST_RSI_STATUS,
                "rsi_error": LATEST_RSI_ERROR,
                "rsi_enabled": RSI_ENABLED,
                "rsi_last_fetch_at": LATEST_RSI_LAST_FETCH_AT,
                "rsi_last_attempt_at": LATEST_RSI_LAST_ATTEMPT_AT,
                "rsi_last_success_at": LATEST_RSI_LAST_SUCCESS_AT,
                "rsi_source": dict(LATEST_RSI_SOURCE or {}),
            }
            if check_completed:
                next_existing.update({
                    "price_per_token_buy": round(price_buy, 8) if price_buy else None,
                    "price_per_token_sell": round(price_sell, 8) if price_sell else None,
                    "token_received": round(token_received, 8) if token_received else None,
                    "usdc_returned": round(usdc_returned, 8) if usdc_returned else None,
                })
            existing.update(next_existing)
            if check_completed:
                existing["scheduler"] = {
                    "enabled": True,
                    "last_checked_mint": OUTPUT_MINT,
                    "last_checked_at": timestamp,
                    "token_count": len(token_states),
                }
            atomic_write_json(shared_json_path, existing)
            TOKEN_CHANGED_SINCE_LAST_WRITE = False
            return timestamp
    except Exception as e:
        print(f"Failed to write shared status file: {e}", flush=True)

def serialize_trigger_times(trigger_map):
    return {k: v.isoformat() for k, v in (trigger_map or {}).items() if hasattr(v, "isoformat")}


def write_scheduled_token_status(token_config, runtime, price_buy, price_sell, token_received, usdc_returned, check_interval, rsi_check_interval, error=None, rsi_enabled=True):
    mint = str((token_config or {}).get("mint") or "").strip()
    if not mint:
        return False
    try:
        with json_file_lock(shared_json_path):
            existing = read_json_file(shared_json_path)
            timestamp = datetime.now(timezone.utc).isoformat()
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            ntfy_topic, ntfy_source = token_ntfy_topic(token_config)
            current_token_state = token_states.get(mint, {})
            if not isinstance(current_token_state, dict):
                current_token_state = {}
            runtime_nonce = str((token_config or {}).get("runtime_nonce") or "")
            persisted_nonce = str(current_token_state.get("runtime_nonce") or "")
            if runtime_nonce and persisted_nonce and runtime_nonce != persisted_nonce:
                print(f"Skipped stale scheduled token state write for {mint}", flush=True)
                return False
            append_token_price_history(existing, mint, timestamp, price_buy, price_sell)
            next_check_at = datetime.fromtimestamp(time.time() + check_interval, timezone.utc).isoformat()
            token_states[mint] = {
                **current_token_state,
                "runtime_nonce": runtime_nonce or persisted_nonce,
                "timestamp": timestamp,
                "last_checked_at": timestamp,
                "next_check_at": next_check_at,
                "name": token_label(token_config),
                "buy_price": round(price_buy, 8) if price_buy else None,
                "sell_price": round(price_sell, 8) if price_sell else None,
                "token_received": round(token_received, 8) if token_received else None,
                "usdc_returned": round(usdc_returned, 8) if usdc_returned else None,
                "latest_rsi": runtime.get("latest_rsi"),
                "latest_rsi_time": runtime.get("latest_rsi_time"),
                "rsi_status": runtime.get("rsi_status"),
                "rsi_error": runtime.get("rsi_error"),
                "rsi_enabled": rsi_enabled,
                "rsi_refresh_nonce": runtime.get("rsi_refresh_nonce", ""),
                "rsi_last_fetch_at": runtime.get("rsi_last_fetch_at"),
                "rsi_last_attempt_at": runtime.get("rsi_last_attempt_at"),
                "rsi_last_success_at": runtime.get("rsi_last_success_at"),
                "rsi_source": dict(runtime.get("rsi_source") or {}),
                "ntfy_topic": normalize_ntfy_topic((token_config or {}).get("ntfy_topic")),
                "ntfy_effective_topic": ntfy_topic,
                "ntfy_topic_source": ntfy_source,
                "check_interval": check_interval,
                "rsi_check_interval": rsi_check_interval,
                "last_triggered_buy": serialize_trigger_times(runtime.get("last_buy_alert")),
                "last_triggered_sell": serialize_trigger_times(runtime.get("last_sell_alert")),
                "last_triggered_rsi": dict(runtime.get("last_triggered_rsi") or {}),
                "error": error,
                "price_status": "error" if error else "ok",
                "price_message": error,
                "price_verification_due_at": None,
            }
            existing["token_states"] = token_states
            existing["token_price_history"] = existing.get("token_price_history", {})
            existing["scheduler"] = {
                "enabled": True,
                "last_checked_mint": mint,
                "last_checked_at": timestamp,
                "token_count": len(token_states),
            }
            atomic_write_json(shared_json_path, existing)
            return True
    except Exception as e:
        print(f"Failed to write scheduled token state for {mint}: {e}", flush=True)
        return False


def write_price_verification_state(
    token_config,
    status,
    message,
    due_at=None,
    advance_schedule=False,
    check_interval=None,
    runtime=None,
    rsi_enabled=None,
):
    mint = str((token_config or {}).get("mint") or "").strip()
    if not mint:
        return False
    try:
        with json_file_lock(shared_json_path):
            existing = read_json_file(shared_json_path)
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            current = token_states.get(mint, {})
            if not isinstance(current, dict):
                current = {}
            runtime_nonce = str((token_config or {}).get("runtime_nonce") or "")
            persisted_nonce = str(current.get("runtime_nonce") or "")
            if runtime_nonce and persisted_nonce and runtime_nonce != persisted_nonce:
                print(f"Skipped stale price verification write for {mint}", flush=True)
                return False

            now = time.time()
            timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
            next_state = {
                **current,
                "runtime_nonce": runtime_nonce or persisted_nonce,
                "name": token_label(token_config),
                "price_status": status,
                "price_message": message,
                "price_verification_due_at": (
                    datetime.fromtimestamp(due_at, timezone.utc).isoformat() if due_at else None
                ),
                "error": message if status == "error" else None,
            }
            if runtime is not None:
                next_state.update({
                    "latest_rsi": runtime.get("latest_rsi"),
                    "latest_rsi_time": runtime.get("latest_rsi_time"),
                    "rsi_status": runtime.get("rsi_status"),
                    "rsi_error": runtime.get("rsi_error"),
                    "rsi_enabled": rsi_enabled,
                    "rsi_refresh_nonce": runtime.get("rsi_refresh_nonce", ""),
                    "rsi_last_fetch_at": runtime.get("rsi_last_fetch_at"),
                    "rsi_last_attempt_at": runtime.get("rsi_last_attempt_at"),
                    "rsi_last_success_at": runtime.get("rsi_last_success_at"),
                    "rsi_source": dict(runtime.get("rsi_source") or {}),
                    "last_triggered_buy": serialize_trigger_times(runtime.get("last_buy_alert")),
                    "last_triggered_sell": serialize_trigger_times(runtime.get("last_sell_alert")),
                    "last_triggered_rsi": dict(runtime.get("last_triggered_rsi") or {}),
                })
            elif mint == OUTPUT_MINT:
                active_rsi_triggers = merge_active_rsi_triggers(current.get("last_triggered_rsi", {}))
                next_state.update({
                    "latest_rsi": LATEST_RSI,
                    "latest_rsi_time": LATEST_RSI_TIME,
                    "rsi_status": LATEST_RSI_STATUS,
                    "rsi_error": LATEST_RSI_ERROR,
                    "rsi_enabled": RSI_ENABLED,
                    "rsi_last_fetch_at": LATEST_RSI_LAST_FETCH_AT,
                    "rsi_last_attempt_at": LATEST_RSI_LAST_ATTEMPT_AT,
                    "rsi_last_success_at": LATEST_RSI_LAST_SUCCESS_AT,
                    "rsi_source": dict(LATEST_RSI_SOURCE or {}),
                    "last_triggered_buy": serialize_trigger_times(last_buy_alert),
                    "last_triggered_sell": serialize_trigger_times(last_sell_alert),
                    "last_triggered_rsi": active_rsi_triggers,
                })
                existing.update({
                    "latest_rsi": LATEST_RSI,
                    "latest_rsi_time": LATEST_RSI_TIME,
                    "rsi_status": LATEST_RSI_STATUS,
                    "rsi_error": LATEST_RSI_ERROR,
                    "rsi_last_fetch_at": LATEST_RSI_LAST_FETCH_AT,
                    "rsi_last_attempt_at": LATEST_RSI_LAST_ATTEMPT_AT,
                    "rsi_last_success_at": LATEST_RSI_LAST_SUCCESS_AT,
                    "rsi_source": dict(LATEST_RSI_SOURCE or {}),
                    "last_triggered_buy": serialize_trigger_times(last_buy_alert),
                    "last_triggered_sell": serialize_trigger_times(last_sell_alert),
                    "last_triggered_rsi": active_rsi_triggers,
                })

            token_states[mint] = next_state
            if advance_schedule:
                interval = check_interval or token_check_interval(token_config)
                next_state.update({
                    "timestamp": timestamp,
                    "last_checked_at": timestamp,
                    "next_check_at": datetime.fromtimestamp(now + interval, timezone.utc).isoformat(),
                })
                existing["scheduler"] = {
                    "enabled": True,
                    "last_checked_mint": mint,
                    "last_checked_at": timestamp,
                    "token_count": len(token_states),
                }
            existing["token_states"] = token_states

            atomic_write_json(shared_json_path, existing)
            return True
    except Exception as exc:
        print(f"Failed to write price verification state for {mint}: {exc}", flush=True)
        return False

def configured_rule_tokens(cfg):
    if not str(os.getenv("JUPITER_API_KEY") or "").strip():
        return []
    if isinstance(cfg, dict) and not coerce_bool(cfg.get("community_rules_enabled"), True):
        return []
    tokens = (cfg or {}).get("tokens")
    if not isinstance(tokens, list):
        return []
    result = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if not coerce_bool(token.get("enabled"), True):
            continue
        mint = str(token.get("mint") or "").strip()
        config = normalize_rules_config(token.get("rules_config"))
        if mint and config["enabled"]:
            next_token = dict(token)
            next_token["rules_config"] = config
            result.append(next_token)
    return result


def cached_quote(input_mint, output_mint, amount_atomic, max_age_seconds=COMMUNITY_RULES_QUOTE_MAX_AGE_SECONDS):
    cached = QUOTE_CACHE.get((str(input_mint), str(output_mint), int(amount_atomic)))
    if not isinstance(cached, dict):
        return None, None
    fetched_at = parse_iso_to_utc(cached.get("fetched_at"))
    if not fetched_at or (datetime.now(timezone.utc) - fetched_at).total_seconds() > max_age_seconds:
        return None, None
    quote = cached.get("quote")
    return (quote, cached.get("fetched_at")) if isinstance(quote, dict) else (None, None)


def fetch_rule_price_impact(token, token_info, config):
    mint = str(token.get("mint") or "").strip()
    decimals = _optional_decimals((token_info or {}).get("decimals"))
    if decimals is None:
        decimals = token_output_decimals(token)
    if decimals is None:
        return {"value": None, "error": "Jupiter did not report token decimals"}

    scenario = config.get("sell_amount_mode", "tracked_usdc")
    token_amount = None
    if scenario == "token_amount":
        token_amount = coerce_float(config.get("sell_token_amount"), 0, minimum=0)
    else:
        tracked_usdc = token_usd_amount(token)
        usdc_atomic = amount_to_atomic(tracked_usdc, resolve_token_decimals(INPUT_MINT, token_input_decimals(token)))
        buy_quote, _buy_checked_at = cached_quote(INPUT_MINT, mint, usdc_atomic)
        if buy_quote:
            token_amount_atomic = int(buy_quote.get("outAmount", "0"))
            token_amount = atomic_to_amount(token_amount_atomic, decimals) if token_amount_atomic > 0 else None
        else:
            usd_price = coerce_float((token_info or {}).get("usdPrice"), 0, minimum=0)
            if usd_price > 0:
                token_amount = tracked_usdc / usd_price

    if not token_amount or token_amount <= 0:
        return {"value": None, "error": "Could not determine the sell amount for this rule"}
    amount_atomic = amount_to_atomic(token_amount, decimals)
    if amount_atomic <= 0:
        return {"value": None, "error": "The configured sell amount is below one atomic token unit"}

    quote, checked_at = cached_quote(mint, INPUT_MINT, amount_atomic)
    if quote is None:
        try:
            quote = get_quote(mint, INPUT_MINT, amount_atomic, community_rules_request=True)
            checked_at = datetime.now(timezone.utc).isoformat()
            QUOTE_CACHE[(mint, str(INPUT_MINT), amount_atomic)] = {"quote": quote, "fetched_at": checked_at}
        except JupiterQuoteError as exc:
            return {"value": None, "error": str(exc)[:180]}

    impact = price_impact_percent(quote)
    if impact is None:
        return {"value": None, "error": "Jupiter did not report price impact for this quote"}
    return {
        "value": impact,
        "checked_at": checked_at,
        "scenario": scenario,
        "token_amount": token_amount,
        "tracked_usdc": token_usd_amount(token) if scenario == "tracked_usdc" else None,
        "estimated_usdc": atomic_to_amount(quote.get("outAmount", 0), resolve_token_decimals(INPUT_MINT, token_input_decimals(token))),
    }


def write_rule_states(updates):
    if not updates:
        return
    try:
        with json_file_lock(shared_json_path):
            existing = read_json_file(shared_json_path)
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            for mint, rules_state in updates.items():
                token_state = token_states.get(mint, {})
                if not isinstance(token_state, dict):
                    token_state = {}
                token_state["rules_state"] = rules_state
                token_states[mint] = token_state
            existing["token_states"] = token_states
            atomic_write_json(shared_json_path, existing)
    except Exception as exc:
        print(f"Failed to write action rule states: {exc}", flush=True)


def refresh_community_rules(cfg, force=False):
    global LAST_COMMUNITY_RULES_REFRESH, LAST_COMMUNITY_RULES_CONFIG_SIGNATURE
    tokens = configured_rule_tokens(cfg)
    if not tokens:
        LAST_COMMUNITY_RULES_CONFIG_SIGNATURE = ""
        return False

    config_signature = f"{(cfg or {}).get('community_rules_refresh_nonce', '')}|" + "|".join(sorted(
        f"{token['mint']}:{rules_config_signature(token['rules_config'])}:{token_usd_amount(token):.12g}"
        for token in tokens
    ))
    now = time.time()
    unchanged = config_signature == LAST_COMMUNITY_RULES_CONFIG_SIGNATURE
    if not force and unchanged and now - LAST_COMMUNITY_RULES_REFRESH < COMMUNITY_RULES_CHECK_INTERVAL:
        return False
    LAST_COMMUNITY_RULES_REFRESH = now
    LAST_COMMUNITY_RULES_CONFIG_SIGNATURE = config_signature

    metadata = {}
    metadata_errors = {}
    for index in range(0, len(tokens), 100):
        batch = tokens[index:index + 100]
        mints = [token["mint"] for token in batch]
        try:
            metadata.update(get_token_information(mints))
        except JupiterQuoteError as exc:
            message = str(exc)[:180]
            for mint in mints:
                metadata_errors[mint] = message

    shared = read_json_file(shared_json_path)
    token_states = shared.get("token_states", {}) if isinstance(shared, dict) else {}
    if not isinstance(token_states, dict):
        token_states = {}
    updates = {}
    rules_global_topic = resolve_global_ntfy_topic(cfg)

    for token in tokens:
        mint = token["mint"]
        config = token["rules_config"]
        info = metadata.get(mint)
        fetch_error = metadata_errors.get(mint, "")
        if info is None and not fetch_error:
            fetch_error = "Jupiter did not return token information for this mint"
        elif isinstance(info, dict) and not info.get("updatedAt") and info.get("lastUpdatedAt"):
            info = {**info, "updatedAt": info.get("lastUpdatedAt")}
        price_info = info
        if isinstance(info, dict):
            source_updated_at = parse_iso_to_utc(info.get("updatedAt"))
            if source_updated_at is None:
                fetch_error = "Jupiter did not report when this token data was updated"
                price_info = {"decimals": info.get("decimals")}
                info = None
            else:
                source_age = (datetime.now(timezone.utc) - source_updated_at).total_seconds()
                if source_age < -300:
                    fetch_error = "Jupiter token data has an invalid future timestamp"
                    price_info = {"decimals": info.get("decimals")}
                    info = None
                elif source_age > COMMUNITY_RULES_SOURCE_MAX_AGE_SECONDS:
                    fetch_error = "Jupiter token data is older than the allowed freshness window"
                    price_info = {"decimals": info.get("decimals")}
                    info = None


        price_impact = None
        if rules_require_price_impact(config):
            price_impact = fetch_rule_price_impact(token, price_info, config)
        evaluated_at = datetime.now(timezone.utc).isoformat()

        evaluation = evaluate_rules(
            config,
            info,
            price_impact,
            evaluated_at=evaluated_at,
            fetch_error=fetch_error,
        )
        previous = token_states.get(mint, {})
        if isinstance(price_info, dict) and price_info.get("updatedAt"):
            evaluation["source_updated_at"] = price_info.get("updatedAt")
        previous_rules = previous.get("rules_state", {}) if isinstance(previous, dict) else {}
        runtime, should_send = advance_alert_state(
            previous_rules,
            evaluation,
            config,
            now=evaluated_at,
            max_gap_seconds=max(180, COMMUNITY_RULES_CHECK_INTERVAL * 3),
        )
        if should_send:
            delivered = send_alert(
                "Action Rules Passed",
                rules_alert_message(token_label(token), mint, runtime),
                token_config=token,
                inherited_topic=rules_global_topic,
            )
            runtime = mark_alert_delivery(
                runtime,
                success=bool(delivered),
                error="No ntfy topic is configured or delivery failed",
                sent_at=evaluated_at,
            )
        try:
            record_rule_history(
                mint,
                config,
                runtime,
                max_gap_seconds=max(180, COMMUNITY_RULES_CHECK_INTERVAL * 3),
            )
        except Exception as exc:
            print(f"Action rule history write failed for {mint}: {exc}", flush=True)
        updates[mint] = runtime

    write_rule_states(updates)
    return True


def check_scheduled_token(token_config, cfg=None, price_only=False):
    mint = str((token_config or {}).get("mint") or "").strip()


    if not mint:
        return

    runtime = get_token_runtime(mint, token_config)
    usd_amount = token_usd_amount(token_config)
    price_interval = token_check_interval(token_config, cfg)
    rsi_interval_minutes = token_rsi_check_interval(token_config, cfg)
    alert_reset_minutes = token_alert_reset_minutes(token_config, cfg)
    rsi_interval = token_rsi_interval(token_config, cfg)
    rsi_reset_enabled = token_rsi_reset_enabled(token_config, cfg)
    buy_alerts = normalize_price_alerts((token_config or {}).get("buy_alerts", []))
    sell_alerts = normalize_price_alerts((token_config or {}).get("sell_alerts", []))
    input_decimals = resolve_token_decimals(INPUT_MINT, token_input_decimals(token_config))
    output_decimals = resolve_token_decimals(mint, token_output_decimals(token_config))
    usdc_lamports = amount_to_atomic(usd_amount, input_decimals)
    label = token_label(token_config)
    error = None

    valid_buy_keys = {f"{float(x):.8f}" for x in buy_alerts}
    for k in list(runtime["last_buy_alert"]):
        if k not in valid_buy_keys:
            runtime["last_buy_alert"].pop(k, None)
    valid_sell_keys = {f"{float(x):.8f}" for x in sell_alerts}
    for k in list(runtime["last_sell_alert"]):
        if k not in valid_sell_keys:
            runtime["last_sell_alert"].pop(k, None)

    print(f"\nScheduler token check: {label} ({short_mint(mint)})", flush=True)
    staged_quote_cache = {}
    token_received_raw = get_out_amount_raw(
        INPUT_MINT, mint, usdc_lamports, cache_result=False, quote_capture=staged_quote_cache
    )
    token_received = atomic_to_amount(token_received_raw, output_decimals) if token_received_raw else None
    usdc_returned_raw = get_out_amount_raw(
        mint, INPUT_MINT, token_received_raw, cache_result=False, quote_capture=staged_quote_cache
    ) if token_received_raw else None
    usdc_returned = atomic_to_amount(usdc_returned_raw, input_decimals) if usdc_returned_raw else None

    price_buy = price_sell = None
    if token_received:
        price_buy = usd_amount / token_received
    else:
        error = "Could not fetch USDC -> token quote"

    if usdc_returned and token_received:
        price_sell = usdc_returned / token_received
    elif not error:
        error = "Could not fetch token -> USDC quote"

    price_decision = assess_price_sample(token_config, price_buy, price_sell)
    if price_decision["status"] == "accepted":
        QUOTE_CACHE.update(staged_quote_cache)
        if price_buy is not None:
            for target in buy_alerts:
                try:
                    alert_price = float(str(target).strip())
                    price_key = f"{alert_price:.8f}"
                    trigger_ready, trigger_time = should_alert(runtime["last_buy_alert"], price_key, alert_reset_minutes)
                    if trigger_ready and price_buy <= alert_price:
                        delivered = send_alert("Buy Price Alert", f"Buy price ${price_buy:.8f} is <= target ${alert_price}", token_config=token_config)
                        if delivered:
                            runtime["last_buy_alert"][price_key] = trigger_time
                except ValueError:
                    continue

        if price_sell is not None:
            for target in sell_alerts:
                try:
                    alert_price = float(str(target).strip())
                    price_key = f"{alert_price:.8f}"
                    trigger_ready, trigger_time = should_alert(runtime["last_sell_alert"], price_key, alert_reset_minutes)
                    if trigger_ready and price_sell >= alert_price:
                        delivered = send_alert("Sell Price Alert", f"Sell price ${price_sell:.8f} is >= target ${alert_price}", token_config=token_config)
                        if delivered:
                            runtime["last_sell_alert"][price_key] = trigger_time
                except ValueError:
                    continue
    rsi_enabled_for_token = token_rsi_enabled(token_config, cfg)
    if not price_only:
        raw_rsi_alerts = token_rsi_alerts_raw(token_config)
        rsi_state = sync_runtime_rsi_state(runtime, raw_rsi_alerts)
        refresh_nonce = str((token_config or {}).get("rsi_refresh_nonce") or "")
        if refresh_nonce != runtime.get("rsi_refresh_nonce", ""):
            runtime["rsi_refresh_nonce"] = refresh_nonce
            clear_runtime_rsi(runtime, "waiting")
        now_utc = datetime.now(timezone.utc)
        last_rsi_at = runtime.get("last_rsi_at")
        disabled_reason = rsi_disabled_reason(token_config, cfg)
        if disabled_reason:
            clear_runtime_rsi(runtime, "disabled", disabled_reason)
        elif last_rsi_at is None or (now_utc - last_rsi_at) >= timedelta(minutes=rsi_interval_minutes):
            runtime["last_rsi_at"] = now_utc
            runtime["rsi_status"] = "waiting"
            runtime["rsi_error"] = None
            try:
                result = get_latest_rsi(
                    api_key=SOLANATRACKER_API_KEY,
                    token=mint,
                    period=14,
                    interval=rsi_interval,
                    include_metadata=True,
                )
                decision, trusted_rsi = apply_trusted_rsi_result(runtime, result, rsi_interval)
                rsi_time = runtime.get("latest_rsi_time")
                if decision == "regressed":
                    print(f"RSI check held for {label}: {runtime['rsi_error']}", flush=True)
                elif trusted_rsi is not None:
                    for key, info in rsi_state.items():
                        direction, val_str = key.split(":", 1)
                        threshold = float(val_str)
                        if info.get("triggered"):
                            if rsi_reset_enabled:
                                crossed_back = (
                                    (direction == "above" and trusted_rsi < threshold) or
                                    (direction == "below" and trusted_rsi > threshold)
                                )
                                if crossed_back:
                                    info["triggered"] = False
                                    runtime["last_triggered_rsi"].pop(key, None)
                            continue

                        should_fire = (
                            (direction == "above" and trusted_rsi > threshold) or
                            (direction == "below" and trusted_rsi < threshold)
                        )
                        if should_fire:
                            delivered = send_alert("RSI Alert", f"RSI({rsi_interval}) = {trusted_rsi:.2f} {direction} {threshold}", token_config=token_config)
                            if delivered:
                                info["triggered"] = True
                                runtime["last_triggered_rsi"][key] = rsi_time
            except Exception as e:
                attempted_at = datetime.now(timezone.utc).isoformat()
                runtime["rsi_status"] = "error"
                runtime["rsi_error"] = str(e)[:180]
                runtime["rsi_last_fetch_at"] = attempted_at
                runtime["rsi_last_attempt_at"] = attempted_at
                print(f"RSI check failed for {label}: {e}", flush=True)

    if price_decision["status"] != "accepted":
        rejected = price_decision["status"] == "rejected"
        write_price_verification_state(
            token_config,
            "error" if rejected else "verifying",
            price_decision.get("message") or "Verifying an unusual Jupiter price move",
            due_at=price_decision.get("due_at"),
            advance_schedule=rejected,
            check_interval=price_interval,
            runtime=runtime,
            rsi_enabled=rsi_enabled_for_token,
        )
        print(f"Price sample for {label}: {price_decision['status']} ({price_decision['reason']})", flush=True)
        return price_decision
    write_scheduled_token_status(
        token_config,
        runtime,
        price_buy,
        price_sell,
        token_received,
        usdc_returned,
        price_interval,
        rsi_interval_minutes,
        error=error,
        rsi_enabled=rsi_enabled_for_token,
    )
    return price_decision

def pick_due_scheduler_token(tokens, cfg):
    global SCHEDULER_CURSOR
    token_by_mint = {
        str(token.get("mint") or "").strip(): token
        for token in (tokens or [])
        if isinstance(token, dict) and str(token.get("mint") or "").strip()
    }
    for mint in list(PRICE_CONFIRMATIONS):
        token = token_by_mint.get(mint)
        if token is None:
            PRICE_CONFIRMATIONS.pop(mint, None)
            SCHEDULER_LAST_CHECK.pop(mint, None)
        elif valid_price_confirmation(token) is None:
            PRICE_CONFIRMATIONS.pop(mint, None)
    if not token_by_mint:
        return None

    now = time.time()
    ordered_tokens = list(token_by_mint.values())
    token_count = len(ordered_tokens)
    normal_due = None
    normal_index = None
    for offset in range(token_count):
        index = (SCHEDULER_CURSOR + offset) % token_count
        token = ordered_tokens[index]
        mint = str(token.get("mint") or "").strip()
        if mint in PRICE_CONFIRMATIONS:
            continue
        interval = token_check_interval(token, cfg)
        if now - scheduler_last_check_for(mint, token) >= interval:
            normal_due = token
            normal_index = index
            break

    due_confirmations = []
    for mint, pending in PRICE_CONFIRMATIONS.items():
        token = token_by_mint.get(mint)
        if token is not None and float(pending.get("due_at") or 0) <= now:
            due_confirmations.append((float(pending.get("due_at") or 0), mint, token, pending))
    due_confirmations.sort(key=lambda item: (item[0], item[1]))

    if due_confirmations:
        _due_at, mint, token, pending = due_confirmations[0]
        if not pending.get("other_token_needed") or normal_due is None:
            result = dict(token)
            result["_price_confirmation"] = True
            try:
                index = next(i for i, row in enumerate(ordered_tokens) if str(row.get("mint") or "").strip() == mint)
                SCHEDULER_CURSOR = (index + 1) % token_count
            except StopIteration:
                pass
            return result

    if normal_due is not None:
        SCHEDULER_CURSOR = (normal_index + 1) % token_count
        return normal_due
    return None


def scheduler_sleep_seconds(tokens, cfg):
    if not tokens:
        for mint in list(PRICE_CONFIRMATIONS):
            SCHEDULER_LAST_CHECK.pop(mint, None)
        PRICE_CONFIRMATIONS.clear()
        return 30
    now = time.time()
    waits = []
    valid_mints = set()
    for token in tokens:
        mint = str(token.get("mint") or "").strip()
        if not mint:
            continue
        valid_mints.add(mint)
        pending = valid_price_confirmation(token)
        if pending is not None:
            waits.append(max(0, float(pending.get("due_at") or now) - now))
            continue
        interval = token_check_interval(token, cfg)
        waits.append(max(0, interval - (now - scheduler_last_check_for(mint, token))))
    for mint in list(PRICE_CONFIRMATIONS):
        if mint not in valid_mints:
            PRICE_CONFIRMATIONS.pop(mint, None)
            SCHEDULER_LAST_CHECK.pop(mint, None)
    if not waits:
        return 30
    return max(5, min(30, min(waits)))

def check_prices(price_only=False):
    global _last_rsi_at, LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR, LATEST_RSI_LAST_FETCH_AT
    global LATEST_RSI_SOURCE, LATEST_RSI_LAST_ATTEMPT_AT, LATEST_RSI_LAST_SUCCESS_AT

    load_dynamic_config()
    active_runtime = get_token_runtime(OUTPUT_MINT, ACTIVE_TOKEN_CONFIG)
    configured_refresh_nonce = str((ACTIVE_TOKEN_CONFIG or {}).get("rsi_refresh_nonce") or "")
    refresh_changed = configured_refresh_nonce != active_runtime.get("rsi_refresh_nonce", "")
    if refresh_changed:
        active_runtime["rsi_refresh_nonce"] = configured_refresh_nonce
        clear_runtime_rsi(active_runtime, "waiting")
    else:
        seed_active_rsi_runtime_from_globals(active_runtime)
    sync_active_rsi_globals(active_runtime)

    input_decimals = resolve_token_decimals(INPUT_MINT, INPUT_DECIMALS)
    output_decimals = resolve_token_decimals(OUTPUT_MINT, OUTPUT_DECIMALS)
    usdc_lamports = amount_to_atomic(USD_AMOUNT, input_decimals)

    local_now = datetime.now().astimezone()
    print(f"\n{local_now.strftime('%Y-%m-%d %H:%M:%S %Z')} - Price Check", flush=True)

    now_utc = datetime.now(timezone.utc)
    if ALERT_RESET_MINUTES > 0:
        cooldown_delta = timedelta(minutes=ALERT_RESET_MINUTES)

        for key in list(last_buy_alert.keys()):
            last_time = last_buy_alert[key]
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if (now_utc - last_time) >= cooldown_delta:
                print(f"Cooldown expired - clearing BUY alert {key}", flush=True)
                del last_buy_alert[key]

        for key in list(last_sell_alert.keys()):
            last_time = last_sell_alert[key]
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if (now_utc - last_time) >= cooldown_delta:
                print(f"Cooldown expired - clearing SELL alert {key}", flush=True)
                del last_sell_alert[key]

    all_buy_keys = [f"{float(b):.8f}" for b in BUY_ALERTS]
    all_sell_keys = [f"{float(s):.8f}" for s in SELL_ALERTS]

    for key in all_buy_keys:
        ready, _ = should_alert(last_buy_alert, key)
        if ready and key in last_buy_alert:
            del last_buy_alert[key]

    for key in all_sell_keys:
        ready, _ = should_alert(last_sell_alert, key)
        if ready and key in last_sell_alert:
            del last_sell_alert[key]

    staged_quote_cache = {}
    token_received_raw = get_out_amount_raw(
        INPUT_MINT, OUTPUT_MINT, usdc_lamports, cache_result=False, quote_capture=staged_quote_cache
    )
    token_received = atomic_to_amount(token_received_raw, output_decimals) if token_received_raw else None
    usdc_returned_raw = get_out_amount_raw(
        OUTPUT_MINT, INPUT_MINT, token_received_raw, cache_result=False, quote_capture=staged_quote_cache
    ) if token_received_raw else None
    usdc_returned = atomic_to_amount(usdc_returned_raw, input_decimals) if usdc_returned_raw else None

    price_buy = price_sell = None
    price_error = None

    if token_received:
        price_buy = USD_AMOUNT / token_received
        print(f"Buying token with ${USD_AMOUNT} USDC:")
        print(f"   Price per token: ${price_buy:.8f}")
        print(f"   Token received: {token_received:.8f}")
    else:
        price_error = "Could not fetch USDC -> token quote"
        print("Could not fetch USDC -> token quote.", flush=True)

    if usdc_returned and token_received:
        price_sell = usdc_returned / token_received
        print(f"\nSelling ${USD_AMOUNT} worth of token:")
        print(f"   Price per token: ${price_sell:.8f}")
        print(f"   USDC received: {usdc_returned:.8f}")
    else:
        if not price_error:
            price_error = "Could not fetch token -> USDC quote"
        print("Could not fetch token -> USDC quote.", flush=True)

    confirmation_token = dict(ACTIVE_TOKEN_CONFIG or {"mint": OUTPUT_MINT})
    confirmation_token["mint"] = OUTPUT_MINT
    price_decision = assess_price_sample(confirmation_token, price_buy, price_sell)
    if price_decision["status"] == "accepted":
        QUOTE_CACHE.update(staged_quote_cache)
        if price_buy is not None:
            for target in BUY_ALERTS:
                try:
                    alert_price = float(str(target).strip())
                    price_key = f"{alert_price:.8f}"
                    trigger_ready, trigger_time = should_alert(last_buy_alert, price_key)
                    if trigger_ready and price_buy <= alert_price:
                        delivered = send_alert("Buy Price Alert", f"Buy price ${price_buy:.8f} is <= target ${alert_price}")
                        if delivered:
                            notify_backend_trigger("buy", alert_price)
                            last_buy_alert[price_key] = trigger_time
                            write_status_json(price_buy, price_sell, token_received, usdc_returned, record_history=False, check_completed=False)
                except ValueError:
                    continue

        if price_sell is not None:
            for target in SELL_ALERTS:
                try:
                    alert_price = float(str(target).strip())
                    price_key = f"{alert_price:.8f}"
                    trigger_ready, trigger_time = should_alert(last_sell_alert, price_key)
                    if trigger_ready and price_sell >= alert_price:
                        delivered = send_alert("Sell Price Alert", f"Sell price ${price_sell:.8f} is >= target ${alert_price}")
                        if delivered:
                            notify_backend_trigger("sell", alert_price)
                            last_sell_alert[price_key] = trigger_time
                            write_status_json(price_buy, price_sell, token_received, usdc_returned, record_history=False, check_completed=False)
                except ValueError:
                    continue
    if not price_only:
        now_utc = datetime.now(timezone.utc)
        disabled_reason = rsi_disabled_reason(ACTIVE_TOKEN_CONFIG or {"rsi_enabled": RSI_ENABLED})
        if disabled_reason:
            clear_runtime_rsi(active_runtime, "disabled", disabled_reason)
            sync_active_rsi_globals(active_runtime)
        elif active_runtime.get("last_rsi_at") is None or (now_utc - active_runtime["last_rsi_at"]) >= timedelta(minutes=RSI_CHECK_INTERVAL):
            active_runtime["last_rsi_at"] = now_utc
            active_runtime["rsi_status"] = "waiting"
            active_runtime["rsi_error"] = None
            sync_active_rsi_globals(active_runtime)
            try:
                result = get_latest_rsi(
                    api_key=SOLANATRACKER_API_KEY,
                    token=OUTPUT_MINT,
                    period=14,
                    interval=RSI_INTERVAL,
                    include_metadata=True,
                )
                decision, trusted_rsi = apply_trusted_rsi_result(active_runtime, result, RSI_INTERVAL)
                rsi_time = active_runtime.get("latest_rsi_time")
                sync_active_rsi_globals(active_runtime)
                if decision == "regressed":
                    print(f"RSI check held: {LATEST_RSI_ERROR}", flush=True)
                elif trusted_rsi is not None:
                    print(f"RSI({RSI_INTERVAL}) = {trusted_rsi:.2f} at {rsi_time} ({decision})", flush=True)

                    for key, info in RSI_STATE.items():
                        direction, val_str = key.split(":")
                        threshold = float(val_str)

                        if info["triggered"]:
                            if RSI_RESET_ENABLED:
                                crossed_back = (
                                    (direction == "above" and trusted_rsi < threshold) or
                                    (direction == "below" and trusted_rsi > threshold)
                                )
                                if crossed_back:
                                    info["triggered"] = False
                                    info.pop("triggered_at", None)
                                    try:
                                        requests.post(
                                            "http://127.0.0.1:8000/api/rsi/reset-alert",
                                            json={"key": key},
                                            timeout=2
                                        )
                                    except Exception:
                                        pass
                            continue

                        should_fire = (
                            (direction == "above" and trusted_rsi > threshold) or
                            (direction == "below" and trusted_rsi < threshold)
                        )
                        if should_fire:
                            msg = f"RSI({RSI_INTERVAL}) = {trusted_rsi:.2f} {direction} {threshold}"
                            print(f"RSI Alert: {msg}", flush=True)
                            delivered = send_alert("RSI Alert", msg)
                            if delivered:
                                info["triggered"] = True
                                info["triggered_at"] = rsi_time
                                notify_backend_rsi_trigger(key, rsi_time)

            except Exception as e:
                attempted_at = datetime.now(timezone.utc).isoformat()
                active_runtime["rsi_status"] = "error"
                active_runtime["rsi_error"] = str(e)[:180]
                active_runtime["rsi_last_fetch_at"] = attempted_at
                active_runtime["rsi_last_attempt_at"] = attempted_at
                sync_active_rsi_globals(active_runtime)
                print(f"RSI check failed: {e}", flush=True)

    if price_decision["status"] != "accepted":
        rejected = price_decision["status"] == "rejected"
        write_price_verification_state(
            confirmation_token,
            "error" if rejected else "verifying",
            price_decision.get("message") or "Verifying an unusual Jupiter price move",
            due_at=price_decision.get("due_at"),
            advance_schedule=rejected,
            check_interval=CHECK_INTERVAL,
        )
        print(f"Price sample for {token_label(confirmation_token)}: {price_decision['status']} ({price_decision['reason']})", flush=True)
        return price_decision
    status_timestamp = write_status_json(
        price_buy,
        price_sell,
        token_received,
        usdc_returned,
        error=price_error,
    )
    print(f"Tracked BUY cooldowns: {list(last_buy_alert.keys())}", flush=True)
    print(f"Tracked SELL cooldowns: {list(last_sell_alert.keys())}", flush=True)

    try:
        requests.post("http://127.0.0.1:8000/api/price", json={
            "timestamp": status_timestamp or datetime.now(timezone.utc).isoformat(),
            "buy_price": price_buy,
            "sell_price": price_sell
        }, timeout=2)
    except Exception as e:
        print(f"Failed to send price to backend: {e}", flush=True)
    return price_decision

def community_rules_worker():
    while True:
        try:
            cfg = read_json_file(config_json_path)
            refresh_community_rules(cfg)
        except Exception as exc:
            print(f"Action rules worker error: {exc}", flush=True)
        time.sleep(max(5, min(10, COMMUNITY_RULES_CHECK_INTERVAL)))


def background_alert_cleaner():
    while True:
        load_dynamic_config()

        if ALERT_RESET_MINUTES > 0:
            now_utc = datetime.now(timezone.utc)
            for alert_dict, label in [(last_buy_alert, "buy"), (last_sell_alert, "sell")]:
                for key, last_time in list(alert_dict.items()):
                    if last_time.tzinfo is None:
                        last_time = last_time.replace(tzinfo=timezone.utc)
                    else:
                        last_time = last_time.astimezone(timezone.utc)

                    if now_utc - last_time >= timedelta(minutes=ALERT_RESET_MINUTES):
                        try:
                            resp = requests.post(
                                "http://127.0.0.1:8000/api/reset-alert",
                                json={"side": label, "price": float(key)},
                                timeout=3,
                            )
                            if resp.ok:
                                alert_dict.pop(key, None)
                                write_status_json(None, None, None, None, check_completed=False)
                        except Exception as e:
                            print(f"Failed to auto-reset {label.upper()} alert {key}: {e}", flush=True)

        time.sleep(max(5, min(CHECK_INTERVAL, 60)))





# Handle reset requests that trigger again immediately if needed.
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

class PnL(BaseModel):
    individual: Dict[str, Any]
    aggregated: Optional[Dict[str, Any]]

_latest_pnl: Dict[str, Any] = {"individual": {}, "aggregated": None}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResetAlert(BaseModel):
    side: str
    price: float

@app.post("/api/reset-alert")
def reset_alert(data: ResetAlert):
    key = f"{data.price:.8f}"
    if data.side == "buy":
        last_buy_alert.pop(key, None)
    elif data.side == "sell":
        last_sell_alert.pop(key, None)
    else:
        return {"success": False, "error": "Invalid side"}

    # Immediately write updated config so it is saved.
    write_status_json(None, None, None, None, check_completed=False)
    return {"success": True}
    

if __name__ == "__main__":
    print("Jupiter Price Monitor started.", flush=True)
    threading.Thread(target=background_alert_cleaner, daemon=True).start()
    threading.Thread(target=community_rules_worker, daemon=True, name="community-rules").start()

    while True:
        sleep_for = 5
        try:
            load_dynamic_config()
            cfg = read_json_file(config_json_path)
            tokens = get_enabled_token_configs(cfg)
            due_token = pick_due_scheduler_token(tokens, cfg)
            if due_token:
                due_mint = str(due_token.get("mint") or "").strip()
                price_only = bool(due_token.get("_price_confirmation"))
                if due_mint == OUTPUT_MINT:
                    check_prices(price_only=price_only)
                else:
                    check_scheduled_token(due_token, cfg, price_only=price_only)
                if due_mint:
                    SCHEDULER_LAST_CHECK[due_mint] = time.time()
                    note_scheduler_token_processed(due_mint)
            sleep_for = scheduler_sleep_seconds(tokens, cfg)
        except Exception as e:
            print(f"Scheduler error: {e}", flush=True)
            sleep_for = max(5, min(CHECK_INTERVAL, 30))
        time.sleep(sleep_for)
