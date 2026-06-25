from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
from typing import Union, Literal
import json
from contextlib import contextmanager
import os
from datetime import datetime, timedelta, timezone
import requests
import threading
import time
from jupiter_quote import quote_out_amount_raw as jupiter_quote_out_amount_raw
from solana_rate_limiter import throttle, configure_rate_limit
from typing import Dict, Any, Optional


class PnL(BaseModel):
    individual: Dict[str, Any]
    aggregated: Optional[Dict[str, Any]]
    token_mint: Optional[str] = None


def normalize_rsi_key(entry: str) -> str:
    """
    Turn "above:30" or "below:70.0" into "above:30.00" / "below:70.00".
    """
    try:
        direction, val_str = str(entry).strip().split(":", 1)
        direction = direction.strip().lower()
        if direction not in {"above", "below"}:
            raise ValueError
        val = float(val_str)
        return f"{direction}:{val:.2f}"
    except:
        raise ValueError(f"Invalid RSI alert format: {entry}")


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_PATH = "/shared/config.json"
STATE_PATH = "/shared/jupiter-latest.json"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
NTFY_TOPIC_ALPHABET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
SOLANATRACKER_PNL_RETRIES = 2
SOLANATRACKER_BASE_URL = os.getenv("SOLANATRACKER_BASE_URL", "https://data.solanatracker.io").rstrip("/")
SOLANATRACKER_PNL_MODE = os.getenv("SOLANATRACKER_PNL_MODE", "adjusted").strip().lower()

def _optional_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_decimals(value):
    decimals = _optional_int(value)
    if decimals is None or decimals < 0 or decimals > 12:
        return None
    return decimals


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
        number = float(value)
        if not (number == number) or number in (float("inf"), float("-inf")):
            raise ValueError
    except (TypeError, ValueError):
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


def solanatracker_features_default():
    return coerce_bool(os.getenv("SOLANATRACKER_ENABLED", os.getenv("SOLANATRACKER_FEATURES_ENABLED")), True)


def solanatracker_api_key_configured():
    return bool(os.getenv("SOLANATRACKER_API_KEY"))


def solanatracker_effective_enabled():
    return bool(state.get("solanatracker_features_enabled", True)) and solanatracker_api_key_configured()


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
    return clamp_float(requests_per_second, 1.0, 0.1, 50.0)


def apply_solanatracker_rate_limit():
    mode = normalize_rate_limit_mode(state.get("solanatracker_rate_limit_mode"), state.get("solanatracker_requests_per_second"))
    state["solanatracker_rate_limit_mode"] = mode
    configure_rate_limit(
        effective_rate_limit_rps(mode, state.get("solanatracker_requests_per_second")),
        enabled=mode != "off",
    )


def read_json_file(path):
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


parse_iso_to_utc = parse_iso_datetime

def clamp_float(value, default, minimum, maximum):
    return coerce_float(value, default, minimum, maximum)


def provided_fields(model):
    fields = getattr(model, "model_fields_set", None)
    if fields is not None:
        return fields
    return getattr(model, "__fields_set__", set())


def normalize_rsi_alerts(entries):
    normalized = set()
    for entry in entries or []:
        try:
            normalized.add(normalize_rsi_key(entry))
        except ValueError:
            continue
    return sorted(normalized)



def short_mint(mint):
    mint = str(mint or "")
    if len(mint) <= 10:
        return mint or "Token"
    return f"{mint[:4]}...{mint[-4:]}"


def is_probable_mint(mint):
    mint = str(mint or "").strip()
    return 32 <= len(mint) <= 44 and all(ch in BASE58_ALPHABET for ch in mint)



def normalize_ntfy_topic(value):
    if value is None:
        return ""
    topic = str(value).strip()
    if not topic:
        return ""
    if len(topic) > 80 or any(ch not in NTFY_TOPIC_ALPHABET for ch in topic):
        raise ValueError("ntfy topic can only use letters, numbers, dash, underscore, or dot")
    return topic


def safe_ntfy_topic(value):
    try:
        return normalize_ntfy_topic(value)
    except ValueError:
        return ""


def optional_bounded_int(value, minimum, maximum):
    number = _optional_int(value)
    if number is None:
        return None
    return max(minimum, min(maximum, number))


def effective_global_ntfy_topic():
    topic = safe_ntfy_topic(state.get("ntfy_topic"))
    if topic:
        return topic
    return safe_ntfy_topic(os.getenv("NTFY_TOPIC"))


def effective_ntfy_topic(token):
    topic = safe_ntfy_topic((token or {}).get("ntfy_topic"))
    if topic:
        return topic
    return effective_global_ntfy_topic()


def ntfy_topic_source(token):
    if safe_ntfy_topic((token or {}).get("ntfy_topic")):
        return "custom"
    if effective_global_ntfy_topic():
        return "inherited"
    return "disabled"


def normalize_wallet_addresses(value):
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = value or []
    wallets = []
    seen = set()
    for raw in raw_values:
        wallet = str(raw or "").strip()
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        wallets.append(wallet)
    return wallets


def active_token_wallets():
    token = get_active_token()
    if token is None:
        return normalize_wallet_addresses(state.get("wallet_addresses", []))
    return normalize_wallet_addresses(token.get("wallet_addresses", []))


def set_active_token_wallets(wallets):
    normalized = normalize_wallet_addresses(wallets)
    state["wallet_addresses"] = normalized
    token = get_active_token()
    if token is not None:
        token["wallet_addresses"] = normalized
    return normalized


def clear_active_pnl_cache():
    prune_pnl_cache(state.get("active_token_mint") or os.getenv("OUTPUT_MINT"))


def normalize_token_entry(entry, fallback=None):
    fallback = fallback or {}
    entry = entry or {}
    mint = str(entry.get("mint") or fallback.get("mint") or "").strip()
    if not is_probable_mint(mint):
        return None

    return {
        "mint": mint,
        "name": str(entry.get("name") or fallback.get("name") or short_mint(mint)).strip()[:40],
        "enabled": bool(entry.get("enabled", fallback.get("enabled", True))),
        "usd_amount": coerce_float(entry.get("usd_amount", fallback.get("usd_amount", state.get("usd_amount", 100.0))), state.get("usd_amount", 100.0), minimum=0.000001),
        "buy_alerts": safe_parse_alerts(entry.get("buy_alerts", fallback.get("buy_alerts", state.get("buy_alerts", [])))),
        "sell_alerts": safe_parse_alerts(entry.get("sell_alerts", fallback.get("sell_alerts", state.get("sell_alerts", [])))),
        "alert_reset_minutes": coerce_int(entry.get("alert_reset_minutes", fallback.get("alert_reset_minutes", state.get("alert_reset_minutes", 0))), state.get("alert_reset_minutes", 0), minimum=0),
        "input_decimals": _optional_decimals(entry.get("input_decimals", fallback.get("input_decimals", state.get("input_decimals")))),
        "output_decimals": _optional_decimals(entry.get("output_decimals", fallback.get("output_decimals", state.get("output_decimals")))),
        "rsi_alerts": normalize_rsi_alerts(entry.get("rsi_alerts", fallback.get("rsi_alerts", state.get("rsi_alerts", [])))),
        "rsi_interval": str(entry.get("rsi_interval", fallback.get("rsi_interval", state.get("rsi_interval", "1s"))) or "1s").strip() or "1s",
        "rsi_reset_enabled": coerce_bool(entry.get("rsi_reset_enabled", fallback.get("rsi_reset_enabled", state.get("rsi_reset_enabled", False))), state.get("rsi_reset_enabled", False)),
        "rsi_enabled": coerce_bool(entry.get("rsi_enabled", fallback.get("rsi_enabled", state.get("rsi_enabled", True))), True),
        "ntfy_topic": safe_ntfy_topic(entry.get("ntfy_topic", fallback.get("ntfy_topic", ""))),
        "wallet_addresses": normalize_wallet_addresses(entry.get("wallet_addresses", fallback.get("wallet_addresses", []))),
        "check_interval": optional_bounded_int(entry.get("check_interval", fallback.get("check_interval")), 5, 86400),
        "rsi_check_interval": optional_bounded_int(entry.get("rsi_check_interval", fallback.get("rsi_check_interval")), 1, 43200),
    }


def current_legacy_token():
    return normalize_token_entry({
        "mint": os.getenv("OUTPUT_MINT"),
        "name": short_mint(os.getenv("OUTPUT_MINT")),
        "enabled": True,
        "usd_amount": state.get("usd_amount", 100.0),
        "buy_alerts": state.get("buy_alerts", []),
        "sell_alerts": state.get("sell_alerts", []),
        "alert_reset_minutes": state.get("alert_reset_minutes", 0),
        "input_decimals": state.get("input_decimals"),
        "output_decimals": state.get("output_decimals"),
        "rsi_alerts": state.get("rsi_alerts", []),
        "rsi_interval": state.get("rsi_interval", "1s"),
        "rsi_reset_enabled": state.get("rsi_reset_enabled", False),
        "rsi_enabled": state.get("rsi_enabled", True),
        "ntfy_topic": "",
        "wallet_addresses": state.get("wallet_addresses", []),
        "check_interval": None,
        "rsi_check_interval": None,
    })


def normalize_tokens(raw_tokens):
    seen = set()
    tokens = []
    for raw in raw_tokens or []:
        token = normalize_token_entry(raw)
        if not token or token["mint"] in seen:
            continue
        seen.add(token["mint"])
        tokens.append(token)
    if not tokens:
        legacy = current_legacy_token()
        if legacy:
            tokens.append(legacy)
    return tokens


def get_active_token():
    tokens = state.get("tokens") or []
    active = state.get("active_token_mint")
    for token in tokens:
        if token.get("mint") == active:
            return token
    return tokens[0] if tokens else None


def apply_active_token_to_legacy():
    token = get_active_token()
    if not token:
        return
    state["active_token_mint"] = token["mint"]
    state["usd_amount"] = token["usd_amount"]
    state["buy_alerts"] = token["buy_alerts"]
    state["sell_alerts"] = token["sell_alerts"]
    state["alert_reset_minutes"] = coerce_int(token.get("alert_reset_minutes"), state.get("alert_reset_minutes", 0), minimum=0)
    state["input_decimals"] = token["input_decimals"]
    state["output_decimals"] = token["output_decimals"]
    state["rsi_alerts"] = token["rsi_alerts"]
    state["rsi_interval"] = token.get("rsi_interval", state.get("rsi_interval", "1s"))
    state["rsi_reset_enabled"] = coerce_bool(token.get("rsi_reset_enabled"), state.get("rsi_reset_enabled", False))
    state["rsi_enabled"] = coerce_bool(token.get("rsi_enabled"), state.get("rsi_enabled", True))
    state["wallet_addresses"] = normalize_wallet_addresses(token.get("wallet_addresses", []))


def sync_legacy_to_active_token():
    token = get_active_token()
    if not token:
        return
    token.update({
        "usd_amount": state["usd_amount"],
        "buy_alerts": safe_parse_alerts(state["buy_alerts"]),
        "sell_alerts": safe_parse_alerts(state["sell_alerts"]),
        "alert_reset_minutes": coerce_int(state.get("alert_reset_minutes"), 0, minimum=0),
        "input_decimals": _optional_decimals(state["input_decimals"]),
        "output_decimals": _optional_decimals(state["output_decimals"]),
        "rsi_alerts": normalize_rsi_alerts(state["rsi_alerts"]),
        "rsi_interval": str(state.get("rsi_interval", "1s") or "1s"),
        "rsi_reset_enabled": coerce_bool(state.get("rsi_reset_enabled"), False),
        "rsi_enabled": coerce_bool(state.get("rsi_enabled"), True),
        "wallet_addresses": normalize_wallet_addresses(state.get("wallet_addresses", [])),
    })


def effective_active_rsi_check_interval():
    token = get_active_token()
    value = token.get("rsi_check_interval") if token else None
    return optional_bounded_int(value, 1, 43200) or state["rsi_check_interval"]

PRICE_HISTORY_RETENTION_HOURS = 24
PRICE_HISTORY_MAX_POINTS_PER_TOKEN = 3000


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


def append_token_price_history(existing, mint, timestamp, buy_price, sell_price):
    mint = str(mint or "").strip()
    if not mint:
        return []
    histories = existing.get("token_price_history", {}) if isinstance(existing, dict) else {}
    if not isinstance(histories, dict):
        histories = {}
    points = prune_history_points(histories.get(mint, []))
    clean_buy = _history_price(buy_price)
    clean_sell = _history_price(sell_price)
    if clean_buy is not None or clean_sell is not None:
        point_time = parse_iso_to_utc(timestamp) or datetime.now(timezone.utc)
        point = {
            "timestamp": point_time.isoformat(),
            "buy_price": clean_buy,
            "sell_price": clean_sell,
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



def is_rsi_warmup_message(message):
    text = str(message or "").lower()
    return any(fragment in text for fragment in (
        "not enough bars",
        "not enough data for rsi",
        "no rsi candles returned",
        "no valid rsi candles returned",
        "no non-zero volume bars",
    ))

def get_token_state_summary():
    latest = read_json_file(STATE_PATH)
    token_states = latest.get("token_states", {}) if isinstance(latest, dict) else {}
    summary = []
    for token in state.get("tokens", []):
        token_state = token_states.get(token["mint"], {})
        effective_check_interval = optional_bounded_int(token.get("check_interval"), 5, 86400) or state["check_interval"]
        effective_rsi_check_interval = optional_bounded_int(token.get("rsi_check_interval"), 1, 43200) or state["rsi_check_interval"]
        rsi_enabled = coerce_bool(token.get("rsi_enabled"), True)
        rsi_error = token_state.get("rsi_error")
        rsi_status = token_state.get("rsi_status")
        rsi_value = token_state.get("latest_rsi")
        if not state.get("solanatracker_features_enabled", True):
            rsi_status = "disabled"
            rsi_error = "SolanaTracker disabled in settings"
            rsi_value = None
        elif not solanatracker_api_key_configured():
            rsi_status = "disabled"
            rsi_error = "SolanaTracker API key is not configured"
            rsi_value = None
        elif not rsi_enabled:
            rsi_status = "disabled"
            rsi_error = "RSI disabled for this token"
            rsi_value = None
        elif is_rsi_warmup_message(rsi_error) and rsi_status == "error":
            rsi_status = "waiting"
        summary.append({
            "mint": token["mint"],
            "name": token.get("name") or short_mint(token["mint"]),
            "enabled": token.get("enabled", True),
            "buy_price": token_state.get("buy_price"),
            "sell_price": token_state.get("sell_price"),
            "rsi": rsi_value,
            "rsi_status": rsi_status,
            "last_checked": token_state.get("timestamp"),
            "next_check_at": token_state.get("next_check_at"),
            "error": token_state.get("error") or (None if rsi_status == "disabled" or is_rsi_warmup_message(rsi_error) else rsi_error),
            "ntfy_topic": token.get("ntfy_topic", ""),
            "ntfy_effective_topic": effective_ntfy_topic(token),
            "ntfy_topic_source": ntfy_topic_source(token),
            "check_interval": token.get("check_interval"),
            "rsi_check_interval": token.get("rsi_check_interval"),
            "alert_reset_minutes": token.get("alert_reset_minutes"),
            "rsi_interval": token.get("rsi_interval"),
            "rsi_reset_enabled": token.get("rsi_reset_enabled"),
            "rsi_enabled": rsi_enabled,
            "effective_check_interval": effective_check_interval,
            "effective_rsi_check_interval": effective_rsi_check_interval,
            "active": token["mint"] == state.get("active_token_mint"),
        })
    return summary

def quote_out_amount_raw(input_mint, output_mint, amount):
    return jupiter_quote_out_amount_raw(input_mint, output_mint, amount)



def post_ntfy(topic, title, body):
    topic = normalize_ntfy_topic(topic)
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    resp = requests.post(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Content-Type": "text/plain; charset=utf-8"},
        timeout=10,
    )
    resp.raise_for_status()


def find_token_or_404(mint):
    clean_mint = str(mint or "").strip()
    for token in state.get("tokens", []):
        if token.get("mint") == clean_mint:
            return token
    raise HTTPException(status_code=404, detail="Token not found")

def validate_token_with_jupiter(mint, usd_amount=None):
    mint = str(mint or "").strip()
    if not is_probable_mint(mint):
        raise HTTPException(status_code=400, detail="Invalid Solana token mint")
    amount = coerce_float(usd_amount, state.get("usd_amount", 100.0), minimum=0.000001)
    input_decimals = state.get("input_decimals") if state.get("input_decimals") is not None else 6
    usdc_amount = int(amount * (10 ** input_decimals))
    out_raw = quote_out_amount_raw(USDC_MINT, mint, usdc_amount)
    reverse_raw = quote_out_amount_raw(mint, USDC_MINT, out_raw)
    return {
        "mint": mint,
        "name": short_mint(mint),
        "valid": True,
        "sample_out_amount": str(out_raw),
        "sample_return_amount": str(reverse_raw),
    }
state = {
    "usd_amount": 100.0,
    "buy_alerts": [],
    "sell_alerts": [],
    "latest_prices": [],
    "token_price_history": {},
    "alert_reset_minutes": 0,
    "check_interval": coerce_int(os.getenv("CHECK_INTERVAL"), 60, minimum=5),
    "solanatracker_rate_limit_mode": normalize_rate_limit_mode(os.getenv("SOLANATRACKER_RATE_LIMIT_MODE"), os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND")),
    "solanatracker_requests_per_second": clamp_float(os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND", "1"), 1.0, 0.1, 50.0),
    "solanatracker_features_enabled": solanatracker_features_default(),
    "ntfy_topic": "",
    "input_decimals": _optional_decimals(os.getenv("INPUT_DECIMALS")),
    "output_decimals": _optional_decimals(os.getenv("OUTPUT_DECIMALS")),
    "last_triggered_buy": {},
    "last_triggered_sell": {},

    # RSI CONFIG
    "rsi_alerts": [],                   # list of floats
    "last_triggered_rsi": {},           # map "above:70.00" to ISO timestamp
    "rsi_interval": os.getenv("RSI_INTERVAL", "1s"),
    "rsi_check_interval": coerce_int(os.getenv("RSI_CHECK_INTERVAL"), 4, minimum=1),
    "rsi_reset_enabled": coerce_bool(os.getenv("RSI_RESET_ENABLED"), False),
    "rsi_enabled": coerce_bool(os.getenv("RSI_ENABLED"), True),
    # WALLET CONFIG
    "wallet_addresses": [],               # list of Solana wallet strings
    "wallet_refresh_minutes": 120,         # default refresh interval
    "schema_version": 3,
    "tokens": [],
    "active_token_mint": os.getenv("OUTPUT_MINT"),
}


# in-memory store
_latest_pnl: Dict[str, Any] = {"individual": {}, "aggregated": None, "token_mint": None}
_latest_pnl_by_token: Dict[str, Any] = {}


def pnl_payload_dict(pnl: PnL):
    if hasattr(pnl, "model_dump"):
        return pnl.model_dump()
    return pnl.dict()


def empty_stored_pnl(token_mint=None):
    return {"individual": {}, "aggregated": None, "token_mint": token_mint}


@app.post("/api/pnl")
async def write_pnl(pnl: PnL):
    """
    Frontend fetchPnl() calls this to save the freshly computed
    individual + aggregated PnL into our in-memory store.
    """
    global _latest_pnl
    payload = pnl_payload_dict(pnl)
    token_mint = str(payload.get("token_mint") or state.get("active_token_mint") or os.getenv("OUTPUT_MINT") or "").strip()
    payload["token_mint"] = token_mint or None
    _latest_pnl = payload
    if token_mint:
        _latest_pnl_by_token[token_mint] = payload
    return {"ok": True}


@app.get("/api/pnl", response_model=PnL)
async def read_pnl(token: Optional[str] = None):
    """
    Dashboard hydrates wallet PnL for the active token only.
    """
    token_mint = str(token or state.get("active_token_mint") or os.getenv("OUTPUT_MINT") or "").strip()
    if token_mint and token_mint in _latest_pnl_by_token:
        return _latest_pnl_by_token[token_mint]
    if _latest_pnl.get("token_mint") == token_mint:
        return _latest_pnl
    return empty_stored_pnl(token_mint or None)


def prune_pnl_cache(token_mint):
    token_mint = str(token_mint or "").strip()
    if token_mint:
        _latest_pnl_by_token.pop(token_mint, None)
    if _latest_pnl.get("token_mint") == token_mint:
        _latest_pnl.clear()
        _latest_pnl.update(empty_stored_pnl(None))


def safe_parse_alerts(value):
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = value or []

    alerts = set()
    for raw in raw_values:
        try:
            price = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if price > 0:
            alerts.add(price)
    return sorted(alerts)


def load_env_defaults():
    try:
        state["usd_amount"] = coerce_float(os.getenv("USD_AMOUNT"), state["usd_amount"], minimum=0.000001)
        state["buy_alerts"] = safe_parse_alerts(os.getenv("BUY_ALERTS", ""))
        state["sell_alerts"] = safe_parse_alerts(os.getenv("SELL_ALERTS", ""))
        state["alert_reset_minutes"] = coerce_int(os.getenv("ALERT_RESET_MINUTES"), state["alert_reset_minutes"], minimum=0)
        state["check_interval"] = coerce_int(os.getenv("CHECK_INTERVAL"), state["check_interval"], minimum=5)
        state["solanatracker_rate_limit_mode"] = normalize_rate_limit_mode(os.getenv("SOLANATRACKER_RATE_LIMIT_MODE", state["solanatracker_rate_limit_mode"]), os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND", state["solanatracker_requests_per_second"]))
        state["solanatracker_requests_per_second"] = clamp_float(os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND", state["solanatracker_requests_per_second"]), 1.0, 0.1, 50.0)
        state["solanatracker_features_enabled"] = coerce_bool(os.getenv("SOLANATRACKER_ENABLED", os.getenv("SOLANATRACKER_FEATURES_ENABLED", state["solanatracker_features_enabled"])), state["solanatracker_features_enabled"])
        apply_solanatracker_rate_limit()
        state["input_decimals"] = _optional_decimals(os.getenv("INPUT_DECIMALS", state["input_decimals"] or ""))
        state["output_decimals"] = _optional_decimals(os.getenv("OUTPUT_DECIMALS", state["output_decimals"] or ""))

        raw = [s.strip() for s in os.getenv("RSI_ALERTS", "").split(",") if s.strip()]
        state["rsi_alerts"] = normalize_rsi_alerts(raw)
        state["rsi_interval"] = os.getenv("RSI_INTERVAL", state["rsi_interval"])
        state["rsi_check_interval"] = coerce_int(os.getenv("RSI_CHECK_INTERVAL"), state["rsi_check_interval"], minimum=1)
        state["rsi_reset_enabled"] = coerce_bool(os.getenv("RSI_RESET_ENABLED", state["rsi_reset_enabled"]), state["rsi_reset_enabled"])
        state["rsi_enabled"] = coerce_bool(os.getenv("RSI_ENABLED", state["rsi_enabled"]), state["rsi_enabled"])

        raw_wallets = os.getenv("WALLET_ADDRESSES", "")
        state["wallet_addresses"] = normalize_wallet_addresses(raw_wallets)
        state["wallet_refresh_minutes"] = coerce_int(os.getenv("WALLET_REFRESH_MINUTES"), state["wallet_refresh_minutes"], minimum=1)
        if not state["tokens"]:
            state["tokens"] = normalize_tokens([])
            state["active_token_mint"] = state["tokens"][0]["mint"] if state["tokens"] else None
            apply_active_token_to_legacy()
    except Exception as e:
        print(f"Failed to load ENV defaults: {e}")

def load_state():
    cfg = read_json_file(CONFIG_PATH)
    if cfg:
        try:
            state["usd_amount"] = coerce_float(cfg.get("usd_amount", state["usd_amount"]), state["usd_amount"], minimum=0.000001)
            state["buy_alerts"] = safe_parse_alerts(cfg.get("buy_alerts", state["buy_alerts"]))
            state["sell_alerts"] = safe_parse_alerts(cfg.get("sell_alerts", state["sell_alerts"]))
            state["alert_reset_minutes"] = coerce_int(cfg.get("alert_reset_minutes", state["alert_reset_minutes"]), state["alert_reset_minutes"], minimum=0)
            state["check_interval"] = coerce_int(cfg.get("check_interval", state["check_interval"]), state["check_interval"], minimum=5)
            state["solanatracker_rate_limit_mode"] = normalize_rate_limit_mode(cfg.get("solanatracker_rate_limit_mode", state["solanatracker_rate_limit_mode"]), cfg.get("solanatracker_requests_per_second", state["solanatracker_requests_per_second"]))
            state["solanatracker_requests_per_second"] = clamp_float(cfg.get("solanatracker_requests_per_second", state["solanatracker_requests_per_second"]), state["solanatracker_requests_per_second"], 0.1, 50.0)
            state["solanatracker_features_enabled"] = coerce_bool(cfg.get("solanatracker_features_enabled", state["solanatracker_features_enabled"]), state["solanatracker_features_enabled"])
            apply_solanatracker_rate_limit()
            state["ntfy_topic"] = safe_ntfy_topic(cfg.get("ntfy_topic", state.get("ntfy_topic", "")))
            state["input_decimals"] = _optional_decimals(cfg.get("input_decimals", state["input_decimals"]))
            state["output_decimals"] = _optional_decimals(cfg.get("output_decimals", state["output_decimals"]))

            state["rsi_alerts"] = normalize_rsi_alerts(cfg.get("rsi_alerts", state["rsi_alerts"]))
            state["rsi_interval"] = cfg.get("rsi_interval", state["rsi_interval"])
            state["rsi_check_interval"] = coerce_int(cfg.get("rsi_check_interval", state["rsi_check_interval"]), state["rsi_check_interval"], minimum=1)
            state["rsi_reset_enabled"] = coerce_bool(cfg.get("rsi_reset_enabled", state["rsi_reset_enabled"]), state["rsi_reset_enabled"])
            state["rsi_enabled"] = coerce_bool(cfg.get("rsi_enabled", state["rsi_enabled"]), state["rsi_enabled"])

            cfg_wallets = normalize_wallet_addresses(cfg.get("wallet_addresses", state["wallet_addresses"]))
            state["wallet_addresses"] = cfg_wallets
            state["wallet_refresh_minutes"] = coerce_int(cfg.get("wallet_refresh_minutes", state["wallet_refresh_minutes"]), state["wallet_refresh_minutes"], minimum=1)
            raw_tokens = cfg.get("tokens", [])
            tokens_have_wallets = any(isinstance(t, dict) and "wallet_addresses" in t for t in raw_tokens)
            state["tokens"] = normalize_tokens(raw_tokens)
            state["active_token_mint"] = cfg.get("active_token_mint") or state["active_token_mint"]
            if state["tokens"] and state["active_token_mint"] not in {t["mint"] for t in state["tokens"]}:
                state["active_token_mint"] = state["tokens"][0]["mint"]
            if cfg_wallets and state["tokens"] and not tokens_have_wallets:
                active_mint = state["active_token_mint"] or state["tokens"][0]["mint"]
                for token in state["tokens"]:
                    if token["mint"] == active_mint:
                        token["wallet_addresses"] = cfg_wallets
                        break
            apply_active_token_to_legacy()
        except Exception as e:
            print(f"Failed to load config.json: {e}")

    s = read_json_file(STATE_PATH)
    if s:
        state["token_price_history"] = s.get("token_price_history", {}) if isinstance(s.get("token_price_history", {}), dict) else {}
        state["latest_prices"] = get_token_price_history(s, state.get("active_token_mint"))
        state["last_triggered_buy"] = s.get("last_triggered_buy", {})
        state["last_triggered_sell"] = s.get("last_triggered_sell", {})
        state["last_triggered_rsi"] = s.get("last_triggered_rsi", {})

def write_config():
    try:
        sync_legacy_to_active_token()
        atomic_write_json(CONFIG_PATH, {
            "usd_amount": state["usd_amount"],
            "buy_alerts": state["buy_alerts"],
            "sell_alerts": state["sell_alerts"],
            "alert_reset_minutes": state["alert_reset_minutes"],
            "check_interval": state["check_interval"],
            "solanatracker_rate_limit_mode": state["solanatracker_rate_limit_mode"],
            "solanatracker_requests_per_second": state["solanatracker_requests_per_second"],
            "solanatracker_features_enabled": state["solanatracker_features_enabled"],
            "ntfy_topic": safe_ntfy_topic(state.get("ntfy_topic")),
            "input_decimals": state["input_decimals"],
            "output_decimals": state["output_decimals"],
            "rsi_alerts": state["rsi_alerts"],
            "rsi_interval": state["rsi_interval"],
            "rsi_check_interval": state["rsi_check_interval"],
            "rsi_reset_enabled": state["rsi_reset_enabled"],
            "rsi_enabled": state["rsi_enabled"],
            "wallet_addresses": normalize_wallet_addresses(state["wallet_addresses"]),
            "wallet_refresh_minutes": state["wallet_refresh_minutes"],
            "schema_version": 3,
            "tokens": state["tokens"],
            "active_token_mint": state["active_token_mint"],
        })
    except Exception as e:
        print(f"Failed to write config.json: {e}")

def write_state(include_triggers=True):
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            histories = existing.get("token_price_history", {})
            if not isinstance(histories, dict):
                histories = {}
            state_histories = state.get("token_price_history", {})
            if isinstance(state_histories, dict):
                for mint, points in state_histories.items():
                    if not isinstance(points, list):
                        continue
                    if len(points) >= len(histories.get(mint, [])):
                        histories[mint] = prune_history_points(points)
            existing.update({
                "latest_prices": state["latest_prices"],
                "token_price_history": histories,
            })
            if include_triggers:
                existing.update({
                    "last_triggered_buy": state["last_triggered_buy"],
                    "last_triggered_sell": state["last_triggered_sell"],
                    "last_triggered_rsi": state["last_triggered_rsi"],
                })
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        print(f"Failed to write jupiter-latest.json: {e}")



def update_active_token_trigger_cache(side, key, timestamp=None, remove=False):
    """Keep manual trigger changes in sync with the active token cache."""
    field_by_side = {
        "buy": "last_triggered_buy",
        "sell": "last_triggered_sell",
        "rsi": "last_triggered_rsi",
    }
    field = field_by_side.get(str(side or "").strip().lower())
    active_mint = state.get("active_token_mint") or os.getenv("OUTPUT_MINT")
    if not field or not active_mint or not key:
        return
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            token_state = token_states.get(active_mint, {})
            if not isinstance(token_state, dict):
                token_state = {}
            triggers = token_state.get(field, {})
            if not isinstance(triggers, dict):
                triggers = {}
            if remove:
                triggers.pop(key, None)
            else:
                triggers[key] = timestamp or datetime.now(timezone.utc).isoformat()
            token_state[field] = triggers
            token_states[active_mint] = token_state
            existing["token_states"] = token_states
            existing[field] = state[field]
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        print(f"Failed to update active token {field or side} trigger state: {e}")


def update_active_token_rsi_trigger(key, timestamp=None, remove=False):
    update_active_token_trigger_cache("rsi", key, timestamp, remove)




def clear_active_price_history():
    active_mint = state.get("active_token_mint") or os.getenv("OUTPUT_MINT")
    state["latest_prices"] = []
    if isinstance(state.get("token_price_history"), dict) and active_mint:
        state["token_price_history"].pop(active_mint, None)
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            histories = existing.get("token_price_history", {})
            if isinstance(histories, dict) and active_mint:
                histories.pop(active_mint, None)
                existing["token_price_history"] = histories
            token_states = existing.get("token_states", {})
            if isinstance(token_states, dict) and active_mint in token_states:
                token_state = token_states.get(active_mint, {})
                if isinstance(token_state, dict):
                    token_state.update({
                        "buy_price": None,
                        "sell_price": None,
                        "token_received": None,
                        "usdc_returned": None,
                        "error": None,
                    })
                    token_states[active_mint] = token_state
                    existing["token_states"] = token_states
            existing.update({
                "latest_prices": [],
                "price_per_token_buy": None,
                "price_per_token_sell": None,
                "token_received": None,
                "usdc_returned": None,
            })
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        print(f"Failed to clear active price history: {e}")

def clear_active_token_rsi_cache():
    active_mint = state.get("active_token_mint") or os.getenv("OUTPUT_MINT")
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            if active_mint:
                token_state = token_states.get(active_mint, {})
                if not isinstance(token_state, dict):
                    token_state = {}
                token_state.update({
                    "latest_rsi": None,
                    "latest_rsi_time": None,
                    "rsi_status": "waiting" if os.getenv("SOLANATRACKER_API_KEY") else "disabled",
                    "rsi_error": None,
                    "rsi_last_fetch_at": None,
                })
                token_states[active_mint] = token_state
            existing["token_states"] = token_states
            existing.update({
                "latest_rsi": None,
                "latest_rsi_time": None,
                "rsi_status": "waiting" if os.getenv("SOLANATRACKER_API_KEY") else "disabled",
                "rsi_error": None,
                "rsi_last_fetch_at": None,
            })
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        print(f"Failed to clear active token RSI cache: {e}")


def clear_active_runtime_cache():
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            active_mint = state.get("active_token_mint")
            token_states = existing.get("token_states", {})
            if not isinstance(token_states, dict):
                token_states = {}
            token_state = token_states.get(active_mint, {}) if active_mint else {}
            if not isinstance(token_state, dict):
                token_state = {}
            active_history = get_token_price_history(existing, active_mint)
            last_point = active_history[-1] if active_history else {}
            last_buy = token_state.get("last_triggered_buy", {})
            last_sell = token_state.get("last_triggered_sell", {})
            last_rsi = token_state.get("last_triggered_rsi", {})
            if not isinstance(last_buy, dict):
                last_buy = {}
            if not isinstance(last_sell, dict):
                last_sell = {}
            if not isinstance(last_rsi, dict):
                last_rsi = {}

            state["latest_prices"] = active_history
            state["last_triggered_buy"] = last_buy
            state["last_triggered_sell"] = last_sell
            state["last_triggered_rsi"] = last_rsi

            existing.update({
                "active_token_mint": active_mint,
                "latest_prices": active_history,
                "price_per_token_buy": last_point.get("buy_price", token_state.get("buy_price")),
                "price_per_token_sell": last_point.get("sell_price", token_state.get("sell_price")),
                "token_received": token_state.get("token_received"),
                "usdc_returned": token_state.get("usdc_returned"),
                "last_triggered_buy": last_buy,
                "last_triggered_sell": last_sell,
                "last_triggered_rsi": last_rsi,
                "latest_rsi": token_state.get("latest_rsi"),
                "latest_rsi_time": token_state.get("latest_rsi_time"),
                "rsi_status": token_state.get("rsi_status") or ("waiting" if os.getenv("SOLANATRACKER_API_KEY") else "disabled"),
                "rsi_error": token_state.get("rsi_error"),
                "rsi_last_fetch_at": token_state.get("rsi_last_fetch_at"),
            })
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        print(f"Failed to restore active token cache: {e}")


def prune_token_state(mint):
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            token_states = existing.get("token_states", {})
            if isinstance(token_states, dict) and mint in token_states:
                token_states.pop(mint, None)
                existing["token_states"] = token_states
                scheduler = existing.get("scheduler")
                if isinstance(scheduler, dict):
                    scheduler["token_count"] = len(token_states)
                    existing["scheduler"] = scheduler
            histories = existing.get("token_price_history", {})
            if isinstance(histories, dict):
                histories.pop(mint, None)
                existing["token_price_history"] = histories
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        print(f"Failed to prune token state for {mint}: {e}")


load_env_defaults()
load_state()
write_config()
write_state()

# Models

class AlertValue(BaseModel):
    value: float

class AlertList(BaseModel):
    values: List[float]

# used by /api/rsi - accepts strings like "above:30" or "below:70"
class RsiAlertList(BaseModel):
    values: List[str]

class PriceData(BaseModel):
    timestamp: str
    buy_price: Optional[float] = None
    sell_price: Optional[float] = None

class ResetConfig(BaseModel):
    minutes: int

class TriggerUpdate(BaseModel):
    side: str
    price: float
    timestamp: str

# for buy/sell resets:
class SingleReset(BaseModel):
    side: Literal["buy","sell"]
    price: float

class RsiTrigger(BaseModel):
    key: str
    timestamp: str

class RsiReset(BaseModel):
    key: str
    

class RsiDelete(BaseModel):
    key: str


class RuntimeSettings(BaseModel):
    check_interval: Optional[int] = None
    rsi_check_interval: Optional[int] = None
    solanatracker_requests_per_second: Optional[float] = None
    solanatracker_rate_limit_mode: Optional[str] = None
    solanatracker_features_enabled: Optional[bool] = None
    ntfy_topic: Optional[str] = None
    input_decimals: Optional[int] = None
    output_decimals: Optional[int] = None



class TokenPayload(BaseModel):
    mint: str
    name: Optional[str] = None
    enabled: bool = True
    ntfy_topic: Optional[str] = None
    check_interval: Optional[int] = None
    rsi_check_interval: Optional[int] = None
    rsi_interval: Optional[str] = None
    rsi_reset_enabled: Optional[bool] = None
    rsi_enabled: Optional[bool] = None


class TokenUpdatePayload(BaseModel):
    name: Optional[str] = None
    ntfy_topic: Optional[str] = None
    check_interval: Optional[int] = None
    rsi_check_interval: Optional[int] = None
    rsi_interval: Optional[str] = None
    rsi_reset_enabled: Optional[bool] = None
    rsi_enabled: Optional[bool] = None


class ActiveTokenPayload(BaseModel):
    mint: str
# Wallet models
class AddressesList(BaseModel):
     values: List[str]

class AddressValue(BaseModel):
     value: str

class PnLBatchRequest(BaseModel):
     token: str
     wallets: List[str]
     



@app.post("/api/rsi/trigger")
async def trigger_rsi(data: RsiTrigger):
    # persist the exact RSI key ("above:70.00" or "below:30.00")
    state["last_triggered_rsi"][data.key] = data.timestamp
    write_state()
    update_active_token_rsi_trigger(data.key, data.timestamp)
    return {"success": True}


@app.get("/api/state")
async def get_state():
    latest = read_json_file(STATE_PATH)
    active_history = get_token_price_history(latest, state.get("active_token_mint") or os.getenv("OUTPUT_MINT"))
    return {
        **state,
        "latest_prices": active_history,
        "active_price_history": active_history,
        "wallet_addresses": active_token_wallets(),
        "wallet_refresh_minutes": state["wallet_refresh_minutes"],
        "input_mint": os.getenv("INPUT_MINT"),
        "output_mint": state.get("active_token_mint") or os.getenv("OUTPUT_MINT"),
        "token_summaries": get_token_state_summary(),
        "scheduler": latest.get("scheduler", {}),
        "solanatracker_effective_requests_per_second": effective_rate_limit_rps(state["solanatracker_rate_limit_mode"], state["solanatracker_requests_per_second"]),
        "solanatracker_api_key_configured": solanatracker_api_key_configured(),
        "solanatracker_features_enabled": state["solanatracker_features_enabled"],
        "solanatracker_enabled": solanatracker_effective_enabled(),
        "ntfy_topic": safe_ntfy_topic(state.get("ntfy_topic")),
        "ntfy_effective_topic": effective_global_ntfy_topic(),
        "ntfy_configured": bool(effective_global_ntfy_topic()),
        "latest_rsi": latest.get("latest_rsi"),
        "latest_rsi_time": latest.get("latest_rsi_time"),
        "rsi_status": latest.get("rsi_status"),
        "rsi_error": latest.get("rsi_error"),
    }



@app.get("/api/tokens")
async def list_tokens():
    return {
        "tokens": state["tokens"],
        "active_token_mint": state["active_token_mint"],
        "summaries": get_token_state_summary(),
    }


@app.post("/api/tokens/validate")
async def validate_token(payload: TokenPayload):
    try:
        result = validate_token_with_jupiter(payload.mint)
        if payload.name:
            result["name"] = payload.name.strip()[:40]
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token is not quoteable on Jupiter: {e}")


@app.post("/api/tokens")
async def add_token(payload: TokenPayload):
    requested_mint = str(payload.mint or "").strip()
    if not is_probable_mint(requested_mint):
        raise HTTPException(status_code=400, detail="Invalid Solana token mint")
    if any(token["mint"] == requested_mint for token in state["tokens"]):
        raise HTTPException(status_code=409, detail="Token already exists")
    try:
        clean_ntfy_topic = normalize_ntfy_topic(payload.ntfy_topic)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    validation = await validate_token(payload)
    mint = validation["mint"]
    token = normalize_token_entry({
        "mint": mint,
        "name": payload.name or validation.get("name") or short_mint(mint),
        "enabled": payload.enabled,
        "usd_amount": state["usd_amount"],
        "buy_alerts": [],
        "sell_alerts": [],
        "alert_reset_minutes": state["alert_reset_minutes"],
        "input_decimals": state["input_decimals"],
        "output_decimals": None,
        "rsi_alerts": [],
        "rsi_interval": state["rsi_interval"],
        "rsi_reset_enabled": state["rsi_reset_enabled"],
        "rsi_enabled": coerce_bool(payload.rsi_enabled, True),
        "ntfy_topic": clean_ntfy_topic,
        "check_interval": payload.check_interval,
        "rsi_check_interval": payload.rsi_check_interval,
    })
    if not token:
        raise HTTPException(status_code=400, detail="Invalid token")
    state["tokens"].append(token)
    if not state.get("active_token_mint"):
        state["active_token_mint"] = token["mint"]
        apply_active_token_to_legacy()
    write_config()
    return {"success": True, "token": token}



@app.patch("/api/tokens/{mint}")
async def update_token(mint: str, payload: TokenUpdatePayload):
    token = find_token_or_404(mint)
    fields = provided_fields(payload)

    if "name" in fields:
        token["name"] = str(payload.name or short_mint(token["mint"])).strip()[:40]
    if "ntfy_topic" in fields:
        try:
            token["ntfy_topic"] = normalize_ntfy_topic(payload.ntfy_topic)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "check_interval" in fields:
        token["check_interval"] = optional_bounded_int(payload.check_interval, 5, 86400)
    if "rsi_check_interval" in fields:
        token["rsi_check_interval"] = optional_bounded_int(payload.rsi_check_interval, 1, 43200)
    if "rsi_interval" in fields:
        token["rsi_interval"] = str(payload.rsi_interval or "1s").strip() or "1s"
    if "rsi_reset_enabled" in fields:
        token["rsi_reset_enabled"] = coerce_bool(payload.rsi_reset_enabled, token.get("rsi_reset_enabled", False))
    if "rsi_enabled" in fields:
        token["rsi_enabled"] = coerce_bool(payload.rsi_enabled, token.get("rsi_enabled", True))

    if token["mint"] == state.get("active_token_mint"):
        apply_active_token_to_legacy()
    write_config()
    return {"success": True, "token": token, "summary": get_token_state_summary()}


@app.post("/api/tokens/{mint}/notify/test")
async def send_token_test_notification(mint: str):
    token = find_token_or_404(mint)
    topic = effective_ntfy_topic(token)
    if not topic:
        raise HTTPException(status_code=400, detail="No ntfy topic is configured for this token")
    try:
        label = token.get("name") or short_mint(token["mint"])
        post_ntfy(topic, f"Test Alert - {label}", f"Test notification for {label} ({token['mint']})")
        return {"success": True, "topic": topic, "source": ntfy_topic_source(token)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Test notification failed: {e}")

@app.post("/api/tokens/active")
async def set_active_token(payload: ActiveTokenPayload):
    mint = payload.mint.strip()
    if not any(token["mint"] == mint for token in state["tokens"]):
        raise HTTPException(status_code=404, detail="Token not found")
    previous_mint = state.get("active_token_mint")
    state["active_token_mint"] = mint
    apply_active_token_to_legacy()
    if previous_mint != mint:
        state["latest_prices"] = []
        state["last_triggered_buy"] = {}
        state["last_triggered_sell"] = {}
        state["last_triggered_rsi"] = {}
    write_config()
    if previous_mint != mint:
        clear_active_runtime_cache()
    else:
        write_state()
    return {"success": True, "active_token_mint": mint}


@app.delete("/api/tokens/{mint}")
async def delete_token(mint: str):
    mint = mint.strip()
    if len(state["tokens"]) <= 1:
        raise HTTPException(status_code=400, detail="At least one token is required")
    before = len(state["tokens"])
    state["tokens"] = [token for token in state["tokens"] if token["mint"] != mint]
    if len(state["tokens"]) == before:
        raise HTTPException(status_code=404, detail="Token not found")
    if state["active_token_mint"] == mint:
        state["active_token_mint"] = state["tokens"][0]["mint"]
        apply_active_token_to_legacy()
        state["latest_prices"] = []
        state["last_triggered_buy"] = {}
        state["last_triggered_sell"] = {}
        state["last_triggered_rsi"] = {}
        clear_active_runtime_cache()
    write_config()
    prune_pnl_cache(mint)
    prune_token_state(mint)
    return {"success": True}


@app.post("/api/settings")
async def update_settings(settings: RuntimeSettings):
    fields = provided_fields(settings)
    old_input_decimals = state.get("input_decimals")
    old_output_decimals = state.get("output_decimals")
    if settings.check_interval is not None:
        if settings.check_interval < 5:
            raise HTTPException(status_code=400, detail="Check interval must be at least 5 seconds")
        state["check_interval"] = settings.check_interval
    if settings.rsi_check_interval is not None:
        if settings.rsi_check_interval < 1:
            raise HTTPException(status_code=400, detail="RSI check interval must be at least 1 minute")
        state["rsi_check_interval"] = settings.rsi_check_interval
    if "solanatracker_rate_limit_mode" in fields:
        raw_mode = str(settings.solanatracker_rate_limit_mode or "safe").strip().lower()
        if raw_mode not in {"safe", "custom", "off", "disabled", "none"}:
            raise HTTPException(status_code=400, detail="Invalid SolanaTracker rate limit mode")
        state["solanatracker_rate_limit_mode"] = normalize_rate_limit_mode(raw_mode)
    if settings.solanatracker_requests_per_second is not None:
        state["solanatracker_requests_per_second"] = clamp_float(
            settings.solanatracker_requests_per_second,
            state["solanatracker_requests_per_second"],
            0.1,
            50.0,
        )
    if "solanatracker_features_enabled" in fields:
        state["solanatracker_features_enabled"] = coerce_bool(settings.solanatracker_features_enabled, state["solanatracker_features_enabled"])
    apply_solanatracker_rate_limit()
    if "ntfy_topic" in fields:
        try:
            state["ntfy_topic"] = normalize_ntfy_topic(settings.ntfy_topic)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "input_decimals" in fields:
        if settings.input_decimals is not None and (settings.input_decimals < 0 or settings.input_decimals > 12):
            raise HTTPException(status_code=400, detail="Input decimals must be between 0 and 12")
        state["input_decimals"] = settings.input_decimals
    if "output_decimals" in fields:
        if settings.output_decimals is not None and (settings.output_decimals < 0 or settings.output_decimals > 12):
            raise HTTPException(status_code=400, detail="Output decimals must be between 0 and 12")
        state["output_decimals"] = settings.output_decimals

    write_config()
    if state.get("input_decimals") != old_input_decimals or state.get("output_decimals") != old_output_decimals:
        clear_active_price_history()
    return {"success": True}


@app.post("/api/notify/test")
async def send_test_notification():
    topic = effective_global_ntfy_topic()
    if not topic:
        raise HTTPException(status_code=400, detail="Global ntfy topic is not configured")
    try:
        post_ntfy(topic, "Test Alert", "Jupiter alert test notification")
        return {"success": True, "topic": topic}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Notification failed: {e}")

@app.post("/api/usd")
async def set_usd(alert: AlertValue):
    if alert.value <= 0:
        raise HTTPException(status_code=400, detail="USD amount must be positive")
    state["usd_amount"] = alert.value
    write_config()
    clear_active_price_history()
    return {"success": True}


@app.post("/api/buy")
async def set_buy_alerts(alerts: AlertList):
    # Combine current alerts with new ones
    combined = set(state["buy_alerts"]) | {value for value in alerts.values if value > 0}
    state["buy_alerts"] = sorted(combined)
    write_config()
    return {"success": True}

@app.post("/api/sell")
async def set_sell_alerts(alerts: AlertList):
    # Combine current alerts with new ones
    combined = set(state["sell_alerts"]) | {value for value in alerts.values if value > 0}
    state["sell_alerts"] = sorted(combined)
    write_config()
    return {"success": True}

@app.delete("/api/buy")
async def delete_buy_alert(alert: AlertValue):
    value = round(alert.value, 8)
    if value in state["buy_alerts"]:
        state["buy_alerts"].remove(value)
        key = f"{value:.8f}"
        state["last_triggered_buy"].pop(key, None)
        write_config()
        write_state()
        update_active_token_trigger_cache("buy", key, remove=True)
        return {"success": True}
    raise HTTPException(status_code=404, detail="Buy alert not found")

@app.delete("/api/sell")
async def delete_sell_alert(alert: AlertValue):
    value = round(alert.value, 8)
    if value in state["sell_alerts"]:
        state["sell_alerts"].remove(value)
        key = f"{value:.8f}"
        state["last_triggered_sell"].pop(key, None)
        write_config()
        write_state()
        update_active_token_trigger_cache("sell", key, remove=True)
        return {"success": True}
    raise HTTPException(status_code=404, detail="Sell alert not found")

@app.post("/api/reset-minutes")
async def set_reset_minutes(config: ResetConfig):
    if config.minutes < 0:
        raise HTTPException(status_code=400, detail="Minutes must be >= 0")
    state["alert_reset_minutes"] = config.minutes
    write_config()
    return {"success": True, "minutes": config.minutes}

@app.post("/api/reset-alert")
async def reset_single_alert(data: SingleReset):
    key = f"{data.price:.8f}"
    now = datetime.now().isoformat()
    if data.side == "buy":
        if key in [f"{v:.8f}" for v in state["buy_alerts"]]:
            state["last_triggered_buy"].pop(key, None)
            write_state()
            update_active_token_trigger_cache("buy", key, remove=True)
            return {"success": True}
        raise HTTPException(status_code=404, detail="Buy alert not found")
    elif data.side == "sell":
        if key in [f"{v:.8f}" for v in state["sell_alerts"]]:
            state["last_triggered_sell"].pop(key, None)
            write_state()
            update_active_token_trigger_cache("sell", key, remove=True)
            return {"success": True}
        raise HTTPException(status_code=404, detail="Sell alert not found")
    raise HTTPException(status_code=400, detail="Invalid alert side")
    

class IntervalConfig(BaseModel):
    interval: str

class ResetMode(BaseModel):
    enabled: bool

@app.post("/api/rsi")
async def add_rsi_alerts(alerts: RsiAlertList):
    # Normalize the incoming entries
    new_keys = set()
    for e in alerts.values:
        try:
            new_keys.add(normalize_rsi_key(e))
        except ValueError:
            continue
    # And re-normalize any existing ones (in case they were raw)
    existing = set(normalize_rsi_alerts(state["rsi_alerts"]))
    state["rsi_alerts"] = sorted(existing | new_keys)
    write_config()
    return {"success": True}

@app.delete("/api/rsi")
async def delete_rsi_alert(data: RsiDelete):
    if data.key in state["rsi_alerts"]:
        state["rsi_alerts"].remove(data.key)
        state["last_triggered_rsi"].pop(data.key, None)
        write_config()
        write_state()
        update_active_token_rsi_trigger(data.key, remove=True)
        return {"success": True}
    raise HTTPException(status_code=404, detail="RSI alert not found")


@app.post("/api/rsi/reset-alert")
async def reset_rsi_alert(data: RsiReset):
    state["last_triggered_rsi"].pop(data.key, None)
    write_state()
    update_active_token_rsi_trigger(data.key, remove=True)
    return {"success": True}


@app.post("/api/rsi/interval")
async def set_rsi_interval(cfg: IntervalConfig):
    state["rsi_interval"] = cfg.interval
    write_config()
    clear_active_token_rsi_cache()
    return {"success": True}

@app.post("/api/rsi/reset-mode")
async def set_rsi_reset_mode(cfg: ResetMode):
    state["rsi_reset_enabled"] = cfg.enabled
    write_config()
    return {"success": True}



@app.post("/api/trigger")
async def update_last_triggered(data: TriggerUpdate):
    price_key = f"{data.price:.8f}"
    if data.side == "buy":
        state["last_triggered_buy"][price_key] = data.timestamp
    elif data.side == "sell":
        state["last_triggered_sell"][price_key] = data.timestamp
    write_state()
    update_active_token_trigger_cache(data.side, price_key, data.timestamp)
    return {"success": True}

@app.post("/api/price")
async def update_price(data: PriceData):
    mint = state.get("active_token_mint") or os.getenv("OUTPUT_MINT")
    try:
        with json_file_lock(STATE_PATH):
            existing = read_json_file(STATE_PATH)
            active_history = append_token_price_history(existing, mint, data.timestamp, data.buy_price, data.sell_price)
            existing["latest_prices"] = active_history
            state["token_price_history"] = existing.get("token_price_history", {})
            state["latest_prices"] = active_history
            atomic_write_json(STATE_PATH, existing)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save price history: {e}")
    return {"success": True}


@app.get("/api/rsi")
async def get_rsi_status():
    cached = read_json_file(STATE_PATH)
    latest_rsi = cached.get("latest_rsi")
    if latest_rsi is not None:
        try:
            latest_rsi = round(float(latest_rsi), 2)
        except (TypeError, ValueError):
            latest_rsi = None

    status = cached.get("rsi_status")
    message = cached.get("rsi_error")
    last_fetch_at = cached.get("rsi_last_fetch_at")
    active_token = get_active_token()
    active_rsi_enabled = coerce_bool((active_token or {}).get("rsi_enabled"), state.get("rsi_enabled", True))

    if not state.get("solanatracker_features_enabled", True):
        status = "disabled"
        latest_rsi = None
        message = "SolanaTracker disabled in settings"
    elif not solanatracker_api_key_configured():
        status = "disabled"
        latest_rsi = None
        message = "SolanaTracker API key is not configured"
    elif not active_rsi_enabled:
        status = "disabled"
        latest_rsi = None
        message = "RSI disabled for this token"
    elif status not in {"ok", "waiting", "error", "stale"}:
        status = "waiting" if latest_rsi is None else "ok"

    fetched_dt = parse_iso_datetime(last_fetch_at or cached.get("latest_rsi_time"))
    if status == "ok" and fetched_dt:
        effective_check_interval = effective_active_rsi_check_interval()
        max_age = timedelta(minutes=max(2, effective_check_interval * 2 + 1))
        if datetime.now(timezone.utc) - fetched_dt > max_age:
            status = "stale"
            message = "RSI cache is stale"

    alerts = {key: {"triggered": True} for key in state["last_triggered_rsi"]}
    for entry in state["rsi_alerts"]:
        try:
            key = normalize_rsi_key(entry)
        except ValueError:
            continue
        alerts.setdefault(key, {"triggered": False})

    return {
        "latest_rsi": latest_rsi,
        "timestamp": cached.get("latest_rsi_time"),
        "interval": state["rsi_interval"],
        "check_interval": state["rsi_check_interval"],
        "effective_check_interval": effective_active_rsi_check_interval(),
        "alerts": alerts,
        "reset_enabled": state["rsi_reset_enabled"],
        "rsi_enabled": active_rsi_enabled,
        "solanatracker_features_enabled": state["solanatracker_features_enabled"],
        "solanatracker_api_key_configured": solanatracker_api_key_configured(),
        "status": status,
        "message": message,
        "last_fetch_at": last_fetch_at,
    }


@app.get("/api/wallets")
async def get_wallets():
    return {"values": active_token_wallets(), "token_mint": state.get("active_token_mint")}

@app.post("/api/wallets")
async def add_wallets(payload: AddressesList):
    current = active_token_wallets()
    next_wallets = current + normalize_wallet_addresses(payload.values)
    set_active_token_wallets(next_wallets)
    clear_active_pnl_cache()
    write_config()
    return {"success": True, "values": active_token_wallets(), "token_mint": state.get("active_token_mint")}

@app.delete("/api/wallets")
async def delete_wallet(payload: AddressValue):
    current = active_token_wallets()
    wallet = str(payload.value or "").strip()
    if wallet in current:
        set_active_token_wallets([w for w in current if w != wallet])
        clear_active_pnl_cache()
        write_config()
        return {"success": True, "values": active_token_wallets(), "token_mint": state.get("active_token_mint")}
    raise HTTPException(status_code=404, detail="Wallet not found")

def _number_or_none(value):
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _number(value, default=0):
    number = _number_or_none(value)
    return default if number is None else number


def _epoch_to_iso(value):
    if isinstance(value, str):
        return value or None
    number = _number_or_none(value)
    if number is None or number <= 0:
        return None
    timestamp = number / 1000 if number > 100_000_000_000 else number
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def empty_pnl_result(status="error", error=None, attempts=0, message=None):
    result = {
        "holding": 0,
        "realized": 0,
        "unrealized": 0,
        "current_value": 0,
        "cost_basis": 0,
        "cost_basis_total": 0,
        "last_trade_time": None,
        "pnl_status": status,
        "pnl_attempts": attempts,
    }
    if error:
        result["pnl_error"] = str(error)[:160]
    if message:
        result["pnl_message"] = str(message)[:160]
    return result


def extract_pnl_result(data, attempts):
    data = data or {}
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    pnl_root = data.get("pnl") if isinstance(data.get("pnl"), dict) else {}
    token_pnl = pnl_root.get("token") if isinstance(pnl_root.get("token"), dict) else pnl_root

    holding = _number(current.get("balance"), _number(data.get("holding"), 0))
    cost_basis_total = _number(current.get("costBasis"), 0)
    avg_cost = _number_or_none(current.get("avgCost"))
    if avg_cost is None:
        avg_cost = cost_basis_total / holding if holding > 0 and cost_basis_total else _number(data.get("cost_basis"), 0)

    realized = _number(token_pnl.get("realized"), _number(data.get("realized"), 0))
    unrealized = _number(token_pnl.get("unrealized"), _number(data.get("unrealized"), 0))
    total = _number(token_pnl.get("total"), realized + unrealized)
    timing = data.get("timing") if isinstance(data.get("timing"), dict) else {}

    return {
        "holding": holding,
        "realized": realized,
        "unrealized": unrealized,
        "total": total,
        "current_value": _number(current.get("value"), _number(data.get("current_value"), 0)),
        "cost_basis": avg_cost,
        "cost_basis_total": cost_basis_total,
        "last_trade_time": _epoch_to_iso(timing.get("lastTrade") or data.get("last_trade_time")),
        "pnl_status": "ok",
        "pnl_attempts": attempts,
    }


def _wallet_from_entry(entry):
    if isinstance(entry, dict):
        return str(entry.get("wallet") or entry.get("address") or entry.get("owner") or "").strip()
    return str(entry or "").strip()


def _token_mint_from_wallet_entry(entry):
    token = entry.get("token") if isinstance(entry.get("token"), dict) else {}
    return str(entry.get("address") or entry.get("mint") or token.get("mint") or token.get("address") or "").strip()


def _extract_basic_wallet_holding(data, token, attempts, source_status="holding_only", source_message=None):
    root = data if isinstance(data, dict) else {}
    if isinstance(root.get("data"), dict):
        root = root.get("data")
    for entry in root.get("tokens", []):
        if not isinstance(entry, dict) or _token_mint_from_wallet_entry(entry) != token:
            continue
        holding = _number(entry.get("balance"), _number(entry.get("amount"), 0))
        price_data = entry.get("price")
        price = _number(price_data.get("usd"), 0) if isinstance(price_data, dict) else _number(price_data, 0)
        current_value = _number(entry.get("value"), holding * price if price else 0)
        if holding <= 0 and current_value <= 0:
            continue
        result = empty_pnl_result(
            status=source_status,
            attempts=attempts,
            message=source_message or "Holding found, but PnL is not available yet",
        )
        result.update({
            "holding": holding,
            "current_value": current_value,
            "price": price,
            "pnl_error": None,
        })
        return result
    return None


def _solanatracker_headers(api_key):
    return {"x-api-key": api_key, "Content-Type": "application/json"}


def _solanatracker_pnl_mode():
    return SOLANATRACKER_PNL_MODE if SOLANATRACKER_PNL_MODE in {"adjusted", "strict", "raw"} else "adjusted"


class SolanaTrackerRequestError(RuntimeError):
    def __init__(self, error, status_code=None):
        super().__init__(str(error))
        self.status_code = status_code


def _request_solanatracker_json(method, url, headers, *, params=None, json_body=None, timeout=10):
    transient_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
    last_error = None
    last_status = None
    for attempt in range(1, SOLANATRACKER_PNL_RETRIES + 1):
        throttle()
        try:
            if method == "POST":
                resp = requests.post(url, headers=headers, params=params, json=json_body, timeout=timeout)
            else:
                resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            last_status = resp.status_code
            if resp.status_code in transient_statuses and attempt < SOLANATRACKER_PNL_RETRIES:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(min(attempt, 2))
                continue
            resp.raise_for_status()
            return resp.json(), attempt
        except requests.RequestException as e:
            last_error = e
            if attempt < SOLANATRACKER_PNL_RETRIES and last_status in transient_statuses:
                time.sleep(min(attempt, 2))
                continue
            break
        except Exception as e:
            last_error = e
            break
    raise SolanaTrackerRequestError(last_error or "SolanaTracker request failed", last_status)


def fetch_basic_wallet_holding(wallet, token, headers, source_status="holding_only", source_error=None):
    try:
        data, attempts = _request_solanatracker_json(
            "GET",
            f"{SOLANATRACKER_BASE_URL}/wallet/{wallet}/basic",
            headers,
            timeout=8,
        )
        result = _extract_basic_wallet_holding(data, token, attempts, source_status, str(source_error) if source_error else None)
        if result:
            return result
        status = "indexing" if source_status == "indexing" else "not_found"
        return empty_pnl_result(status=status, error=source_error or "No holding found", attempts=attempts)
    except Exception as e:
        status = "indexing" if source_status == "indexing" else "error"
        return empty_pnl_result(status=status, error=source_error or e, attempts=SOLANATRACKER_PNL_RETRIES)


def fetch_pnl_batch_results(wallets, token, api_key):
    token = str(token or "").strip()
    normalized_wallets = normalize_wallet_addresses(wallets)
    headers = _solanatracker_headers(api_key)
    results = {}

    for start in range(0, len(normalized_wallets), 200):
        chunk = normalized_wallets[start:start + 200]
        chunk_set = set(chunk)
        try:
            payload, attempts = _request_solanatracker_json(
                "POST",
                f"{SOLANATRACKER_BASE_URL}/v2/pnl/tokens/{token}/positions/batch",
                headers,
                params={"pnlMode": _solanatracker_pnl_mode()},
                json_body={"wallets": chunk},
                timeout=12,
            )
        except SolanaTrackerRequestError as e:
            if e.status_code in {400, 422}:
                try:
                    payload, attempts = _request_solanatracker_json(
                        "POST",
                        f"{SOLANATRACKER_BASE_URL}/v2/pnl/tokens/{token}/positions/batch",
                        headers,
                        params={"pnlMode": _solanatracker_pnl_mode()},
                        json_body=chunk,
                        timeout=12,
                    )
                except Exception:
                    for wallet in chunk:
                        results[wallet] = fetch_basic_wallet_holding(wallet, token, headers, "holding_only", "PnL unavailable")
                    continue
            else:
                for wallet in chunk:
                    results[wallet] = fetch_basic_wallet_holding(wallet, token, headers, "holding_only", "PnL unavailable")
                continue
        except Exception:
            for wallet in chunk:
                results[wallet] = fetch_basic_wallet_holding(wallet, token, headers, "holding_only", "PnL unavailable")
            continue

        root_payload = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        if isinstance(root_payload, dict) and (root_payload.get("indexed") is False or root_payload.get("queued") is True):
            message = root_payload.get("message") or "Wallet PnL is queued for indexing"
            for wallet in chunk:
                results[wallet] = fetch_basic_wallet_holding(wallet, token, headers, "indexing", message)
            continue

        if isinstance(root_payload, list):
            positions = root_payload
            not_found_entries = []
            invalid_entries = []
        else:
            positions = root_payload.get("positions", []) if isinstance(root_payload, dict) else []
            not_found_entries = root_payload.get("notFound", []) if isinstance(root_payload, dict) else []
            invalid_entries = root_payload.get("invalid", []) if isinstance(root_payload, dict) else []

        if isinstance(positions, dict):
            positions = list(positions.values())

        for position in positions or []:
            if not isinstance(position, dict):
                continue
            wallet = _wallet_from_entry(position)
            if not wallet and len(chunk) == 1:
                wallet = chunk[0]
            if wallet:
                results[wallet] = extract_pnl_result(position, attempts)

        invalid_wallets = {_wallet_from_entry(entry) for entry in invalid_entries or []}
        invalid_wallets = {wallet for wallet in invalid_wallets if wallet in chunk_set}
        for wallet in invalid_wallets:
            results[wallet] = empty_pnl_result(status="error", error="Invalid wallet address", attempts=attempts)

        not_found_wallets = {_wallet_from_entry(entry) for entry in not_found_entries or []}
        not_found_wallets = {wallet for wallet in not_found_wallets if wallet in chunk_set}
        missing_wallets = [wallet for wallet in chunk if wallet not in results]
        for wallet in missing_wallets:
            reason = "PnL not found" if wallet in not_found_wallets else "PnL unavailable"
            results[wallet] = fetch_basic_wallet_holding(wallet, token, headers, "holding_only", reason)

    return {wallet: results.get(wallet, empty_pnl_result(status="error", error="No result")) for wallet in normalized_wallets}


@app.post("/api/pnl/batch")
async def get_pnl_batch(payload: PnLBatchRequest):
    if not state.get("solanatracker_features_enabled", True):
        raise HTTPException(status_code=400, detail="SolanaTracker features are disabled")
    api_key = os.getenv("SOLANATRACKER_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="SolanaTracker API key not set")
    token = str(payload.token or "").strip()
    if not is_probable_mint(token):
        raise HTTPException(status_code=400, detail="Invalid Solana token mint")
    wallets = normalize_wallet_addresses(payload.wallets)
    if not wallets:
        return {"individual": {}, "token_mint": token}
    return {"individual": fetch_pnl_batch_results(wallets, token, api_key), "token_mint": token}


@app.get("/api/pnl/{wallet}/{token}")
async def get_pnl(wallet: str, token: str):
    if not state.get("solanatracker_features_enabled", True):
        raise HTTPException(status_code=400, detail="SolanaTracker features are disabled")
    api_key = os.getenv("SOLANATRACKER_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="SolanaTracker API key not set")
    token = str(token or "").strip()
    if not is_probable_mint(token):
        raise HTTPException(status_code=400, detail="Invalid Solana token mint")
    clean_wallet = str(wallet or "").strip()
    if not clean_wallet:
        raise HTTPException(status_code=400, detail="Wallet address is required")
    return fetch_pnl_batch_results([clean_wallet], token, api_key).get(clean_wallet, empty_pnl_result())

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

@app.get("/{full_path:path}")
async def serve_index(full_path: str):
    index_path = os.path.join("frontend", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Page not found")



