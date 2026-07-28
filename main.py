import os
import time
import threading
import requests
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
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
        "last_rsi_at": parse_iso_to_utc(token_state.get("rsi_last_fetch_at") or token_state.get("latest_rsi_time")),
        "latest_rsi": token_state.get("latest_rsi"),
        "latest_rsi_time": token_state.get("latest_rsi_time"),
        "rsi_status": token_state.get("rsi_status") or ("waiting" if solanatracker_effective_enabled() else "disabled"),
        "rsi_error": token_state.get("rsi_error"),
        "rsi_last_fetch_at": token_state.get("rsi_last_fetch_at"),
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
    global OUTPUT_MINT, _last_rsi_at, LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR, LATEST_RSI_LAST_FETCH_AT, TOKEN_CHANGED_SINCE_LAST_WRITE

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
    TOKEN_CHANGED_SINCE_LAST_WRITE = True
    return True


def load_dynamic_config():
    global USD_AMOUNT, BUY_ALERTS, SELL_ALERTS, ALERT_RESET_MINUTES, CHECK_INTERVAL
    global RSI_ALERTS_RAW, RSI_INTERVAL, RSI_RESET_ENABLED, RSI_CHECK_INTERVAL, RSI_ENABLED
    global SOLANATRACKER_RATE_LIMIT_MODE, SOLANATRACKER_REQUESTS_PER_SECOND, SOLANATRACKER_FEATURES_ENABLED, INPUT_DECIMALS, OUTPUT_DECIMALS, OUTPUT_MINT, ACTIVE_TOKEN_CONFIG, NTFY_TOPIC
    global ACTIVE_RSI_REFRESH_NONCE, _last_rsi_at
    global LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR, LATEST_RSI_LAST_FETCH_AT

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

def get_out_amount_raw(input_mint, output_mint, amount_lamports):
    """
    Uses Jupiter Swap V2 order in quote-only mode and returns raw outAmount.
    The caller is responsible for converting raw token units with the output mint decimals.
    """
    try:
        quote = get_quote(input_mint, output_mint, amount_lamports)
        key = (str(input_mint), str(output_mint), int(amount_lamports))
        QUOTE_CACHE[key] = {
            "quote": quote,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
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
                "ntfy_topic": normalize_ntfy_topic((token_config or {}).get("ntfy_topic")),
                "ntfy_effective_topic": ntfy_topic,
                "ntfy_topic_source": ntfy_source,
                "check_interval": check_interval,
                "rsi_check_interval": rsi_check_interval,
                "last_triggered_buy": serialize_trigger_times(runtime.get("last_buy_alert")),
                "last_triggered_sell": serialize_trigger_times(runtime.get("last_sell_alert")),
                "last_triggered_rsi": dict(runtime.get("last_triggered_rsi") or {}),
                "error": error,
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


def check_scheduled_token(token_config, cfg=None):
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
    token_received_raw = get_out_amount_raw(INPUT_MINT, mint, usdc_lamports)
    token_received = atomic_to_amount(token_received_raw, output_decimals) if token_received_raw else None
    usdc_returned_raw = get_out_amount_raw(mint, INPUT_MINT, token_received_raw) if token_received_raw else None
    usdc_returned = atomic_to_amount(usdc_returned_raw, input_decimals) if usdc_returned_raw else None

    price_buy = price_sell = None
    if token_received:
        price_buy = usd_amount / token_received
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
    else:
        error = "Could not fetch USDC -> token quote"

    if usdc_returned and token_received:
        price_sell = usdc_returned / token_received
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
    elif not error:
        error = "Could not fetch token -> USDC quote"

    raw_rsi_alerts = token_rsi_alerts_raw(token_config)
    rsi_state = sync_runtime_rsi_state(runtime, raw_rsi_alerts)
    refresh_nonce = str((token_config or {}).get("rsi_refresh_nonce") or "")
    if refresh_nonce != runtime.get("rsi_refresh_nonce", ""):
        runtime["rsi_refresh_nonce"] = refresh_nonce
        runtime["last_rsi_at"] = None
        runtime["latest_rsi"] = None
        runtime["latest_rsi_time"] = None
        runtime["rsi_last_fetch_at"] = None
    now_utc = datetime.now(timezone.utc)
    last_rsi_at = runtime.get("last_rsi_at")
    rsi_enabled_for_token = token_rsi_enabled(token_config, cfg)
    disabled_reason = rsi_disabled_reason(token_config, cfg)
    if disabled_reason:
        runtime["latest_rsi"] = None
        runtime["latest_rsi_time"] = None
        runtime["rsi_status"] = "disabled"
        runtime["rsi_error"] = disabled_reason
        runtime["rsi_last_fetch_at"] = None
        runtime["last_rsi_at"] = None
    elif last_rsi_at is None or (now_utc - last_rsi_at) >= timedelta(minutes=rsi_interval_minutes):
        runtime["last_rsi_at"] = now_utc
        runtime["rsi_status"] = "waiting"
        runtime["rsi_error"] = None
        try:
            rsi_value, rsi_time = get_latest_rsi(
                api_key=SOLANATRACKER_API_KEY,
                token=mint,
                period=14,
                interval=rsi_interval,
            )
            runtime["latest_rsi"] = round(float(rsi_value), 2)
            runtime["latest_rsi_time"] = rsi_time
            runtime["rsi_status"] = "ok"
            runtime["rsi_error"] = None
            runtime["rsi_last_fetch_at"] = datetime.now(timezone.utc).isoformat()

            for key, info in rsi_state.items():
                direction, val_str = key.split(":", 1)
                threshold = float(val_str)
                if info.get("triggered"):
                    if rsi_reset_enabled:
                        crossed_back = (
                            (direction == "above" and rsi_value < threshold) or
                            (direction == "below" and rsi_value > threshold)
                        )
                        if crossed_back:
                            info["triggered"] = False
                            runtime["last_triggered_rsi"].pop(key, None)
                    continue

                should_fire = (
                    (direction == "above" and rsi_value > threshold) or
                    (direction == "below" and rsi_value < threshold)
                )
                if should_fire:
                    delivered = send_alert("RSI Alert", f"RSI({rsi_interval}) = {rsi_value:.2f} {direction} {threshold}", token_config=token_config)
                    if delivered:
                        info["triggered"] = True
                        runtime["last_triggered_rsi"][key] = rsi_time
        except Exception as e:
            runtime["rsi_status"] = "error"
            runtime["rsi_error"] = str(e)[:180]
            runtime["rsi_last_fetch_at"] = datetime.now(timezone.utc).isoformat()
            print(f"RSI check failed for {label}: {e}", flush=True)

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


def pick_due_scheduler_token(tokens, cfg):
    global SCHEDULER_CURSOR
    if not tokens:
        return None
    now = time.time()
    token_count = len(tokens)
    for offset in range(token_count):
        index = (SCHEDULER_CURSOR + offset) % token_count
        token = tokens[index]
        mint = str(token.get("mint") or "").strip()
        if not mint:
            continue
        interval = token_check_interval(token, cfg)
        if now - scheduler_last_check_for(mint, token) >= interval:
            SCHEDULER_CURSOR = (index + 1) % token_count
            return token
    return None


def scheduler_sleep_seconds(tokens, cfg):
    if not tokens:
        return 30
    now = time.time()
    waits = []
    for token in tokens:
        mint = str(token.get("mint") or "").strip()
        if not mint:
            continue
        interval = token_check_interval(token, cfg)
        waits.append(max(0, interval - (now - scheduler_last_check_for(mint, token))))
    if not waits:
        return 30
    return max(5, min(30, min(waits)))

def check_prices():
    global _last_rsi_at, LATEST_RSI, LATEST_RSI_TIME, LATEST_RSI_STATUS, LATEST_RSI_ERROR, LATEST_RSI_LAST_FETCH_AT

    load_dynamic_config()
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

    token_received_raw = get_out_amount_raw(INPUT_MINT, OUTPUT_MINT, usdc_lamports)
    token_received = atomic_to_amount(token_received_raw, output_decimals) if token_received_raw else None
    usdc_returned_raw = get_out_amount_raw(OUTPUT_MINT, INPUT_MINT, token_received_raw) if token_received_raw else None
    usdc_returned = atomic_to_amount(usdc_returned_raw, input_decimals) if usdc_returned_raw else None

    price_buy = price_sell = None
    price_error = None

    if token_received:
        price_buy = USD_AMOUNT / token_received
        print(f"Buying token with ${USD_AMOUNT} USDC:")
        print(f"   Price per token: ${price_buy:.8f}")
        print(f"   Token received: {token_received:.8f}")

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
                        status_timestamp = write_status_json(price_buy, price_sell, token_received, usdc_returned, record_history=False, check_completed=False)
            except ValueError:
                continue
    else:
        price_error = "Could not fetch USDC -> token quote"
        print("Could not fetch USDC -> token quote.", flush=True)

    if usdc_returned and token_received:
        price_sell = usdc_returned / token_received
        print(f"\nSelling ${USD_AMOUNT} worth of token:")
        print(f"   Price per token: ${price_sell:.8f}")
        print(f"   USDC received: {usdc_returned:.8f}")

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
                        status_timestamp = write_status_json(price_buy, price_sell, token_received, usdc_returned, record_history=False, check_completed=False)
            except ValueError:
                continue
    else:
        if not price_error:
            price_error = "Could not fetch token -> USDC quote"
        print("Could not fetch token -> USDC quote.", flush=True)

    now_utc = datetime.now(timezone.utc)
    disabled_reason = rsi_disabled_reason(ACTIVE_TOKEN_CONFIG or {"rsi_enabled": RSI_ENABLED})
    if disabled_reason:
        LATEST_RSI = None
        LATEST_RSI_TIME = None
        LATEST_RSI_STATUS = "disabled"
        LATEST_RSI_ERROR = disabled_reason
        LATEST_RSI_LAST_FETCH_AT = None
        _last_rsi_at = None
    elif _last_rsi_at is None or (now_utc - _last_rsi_at) >= timedelta(minutes=RSI_CHECK_INTERVAL):
        _last_rsi_at = now_utc
        LATEST_RSI_STATUS = "waiting"
        LATEST_RSI_ERROR = None
        try:
            rsi_value, rsi_time = get_latest_rsi(
                api_key=SOLANATRACKER_API_KEY,
                token=OUTPUT_MINT,
                period=14,
                interval=RSI_INTERVAL
            )
            LATEST_RSI = round(float(rsi_value), 2)
            LATEST_RSI_TIME = rsi_time
            LATEST_RSI_STATUS = "ok"
            LATEST_RSI_ERROR = None
            LATEST_RSI_LAST_FETCH_AT = datetime.now(timezone.utc).isoformat()
            print(f"RSI({RSI_INTERVAL}) = {rsi_value:.2f} at {rsi_time}", flush=True)

            for key, info in RSI_STATE.items():
                direction, val_str = key.split(":")
                threshold = float(val_str)

                if info["triggered"]:
                    if RSI_RESET_ENABLED:
                        crossed_back = (
                            (direction == "above" and rsi_value < threshold) or
                            (direction == "below" and rsi_value > threshold)
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
                    (direction == "above" and rsi_value > threshold) or
                    (direction == "below" and rsi_value < threshold)
                )
                if should_fire:
                    msg = f"RSI({RSI_INTERVAL}) = {rsi_value:.2f} {direction} {threshold}"
                    print(f"RSI Alert: {msg}", flush=True)
                    delivered = send_alert("RSI Alert", msg)
                    if delivered:
                        info["triggered"] = True
                        info["triggered_at"] = rsi_time
                        notify_backend_rsi_trigger(key, rsi_time)

        except Exception as e:
            LATEST_RSI_STATUS = "error"
            LATEST_RSI_ERROR = str(e)[:180]
            LATEST_RSI_LAST_FETCH_AT = datetime.now(timezone.utc).isoformat()
            print(f"RSI check failed: {e}", flush=True)

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
                if due_mint == OUTPUT_MINT:
                    check_prices()
                else:
                    check_scheduled_token(due_token, cfg)
                if due_mint:
                    SCHEDULER_LAST_CHECK[due_mint] = time.time()
            sleep_for = scheduler_sleep_seconds(tokens, cfg)
        except Exception as e:
            print(f"Scheduler error: {e}", flush=True)
            sleep_for = max(5, min(CHECK_INTERVAL, 30))
        time.sleep(sleep_for)
