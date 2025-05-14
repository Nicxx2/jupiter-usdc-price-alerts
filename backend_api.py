from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
from typing import Union, Literal
import json
import os
from datetime import datetime
from rsi_utils import get_latest_rsi


def normalize_rsi_key(entry: str) -> str:
    """
    Turn "above:30" or "below:70.0" into "above:30.00" / "below:70.00".
    """
    try:
        direction, val_str = entry.split(":", 1)
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

state = {
    "usd_amount": 100.0,
    "buy_alerts": [],
    "sell_alerts": [],
    "latest_prices": [],
    "alert_reset_minutes": 0,
    "last_triggered_buy": {},
    "last_triggered_sell": {},

    # ─── RSI CONFIG ───────────────────────────────────────
    "rsi_alerts": [],                   # list of floats
    "last_triggered_rsi": {},           # map "above:70.00" → ISO timestamp
    "rsi_interval": os.getenv("RSI_INTERVAL", "1s"),
    "rsi_reset_enabled": os.getenv("RSI_RESET_ENABLED", "false").lower() == "true",
}

def safe_parse_alerts(value: str):
    try:
        return sorted(set([float(v.strip()) for v in value.split(",") if v.strip()]))
    except:
        return []

def load_env_defaults():
    try:
        state["usd_amount"] = float(os.getenv("USD_AMOUNT", state["usd_amount"]))
        state["buy_alerts"] = safe_parse_alerts(os.getenv("BUY_ALERTS", ""))
        state["sell_alerts"] = safe_parse_alerts(os.getenv("SELL_ALERTS", ""))
        state["alert_reset_minutes"] = int(os.getenv("ALERT_RESET_MINUTES", state["alert_reset_minutes"]))
        # ─── RSI env defaults ──────────────────────────────────
        raw = [s.strip() for s in os.getenv("RSI_ALERTS", "").split(",") if s.strip()]
        state["rsi_alerts"] = sorted({ normalize_rsi_key(e) for e in raw })
        state["rsi_interval"]      = os.getenv("RSI_INTERVAL", state["rsi_interval"])
        state["rsi_reset_enabled"] = os.getenv("RSI_RESET_ENABLED", str(state["rsi_reset_enabled"])).lower() == "true"
    except Exception as e:
        print(f"⚠️ Failed to load ENV defaults: {e}")

def load_state():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
                state["usd_amount"] = cfg.get("usd_amount", state["usd_amount"])
                state["buy_alerts"] = cfg.get("buy_alerts", state["buy_alerts"])
                state["sell_alerts"] = cfg.get("sell_alerts", state["sell_alerts"])
                state["alert_reset_minutes"] = cfg.get("alert_reset_minutes", state["alert_reset_minutes"])
                # ─── RSI CONFIG from config.json ────────────────────
                state["rsi_alerts"]        = cfg.get("rsi_alerts", state["rsi_alerts"])
                state["rsi_alerts"] = sorted({ normalize_rsi_key(e) for e in state["rsi_alerts"] })
                state["rsi_interval"]      = cfg.get("rsi_interval", state["rsi_interval"])
                state["rsi_reset_enabled"] = cfg.get("rsi_reset_enabled", state["rsi_reset_enabled"])
        except Exception as e:
            print(f"⚠️ Failed to load config.json: {e}")

    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                s = json.load(f)
                state["latest_prices"] = s.get("latest_prices", [])
                state["last_triggered_buy"] = s.get("last_triggered_buy", {})
                state["last_triggered_sell"] = s.get("last_triggered_sell", {})

        except Exception as e:
            print(f"⚠️ Failed to load jupiter-latest.json: {e}")

def write_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump({
                "usd_amount":        state["usd_amount"],
                "buy_alerts":        state["buy_alerts"],
                "sell_alerts":       state["sell_alerts"],
                "alert_reset_minutes": state["alert_reset_minutes"],
                # ─── RSI CONFIG ─────────────────────────
                "rsi_alerts":        state["rsi_alerts"],
                "rsi_interval":      state["rsi_interval"],
                "rsi_reset_enabled": state["rsi_reset_enabled"],
            }, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to write config.json: {e}")

def write_state():
    try:
        with open(STATE_PATH, "w") as f:
            json.dump({
                "latest_prices": state["latest_prices"],
                "last_triggered_buy": state["last_triggered_buy"],
                "last_triggered_sell": state["last_triggered_sell"],
                "last_triggered_rsi":   state["last_triggered_rsi"]
            }, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to write jupiter-latest.json: {e}")

load_env_defaults()
load_state()
write_config()
write_state()

# Models

class AlertValue(BaseModel):
    value: float

class AlertList(BaseModel):
    values: List[float]

# used by /api/rsi – accepts strings like "above:30" or "below:70"
class RsiAlertList(BaseModel):
    values: List[str]

class PriceData(BaseModel):
    timestamp: str
    buy_price: float
    sell_price: float

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

@app.post("/api/rsi/trigger")
async def trigger_rsi(data: RsiTrigger):
    # persist the exact RSI key ("above:70.00" or "below:30.00")
    state["last_triggered_rsi"][data.key] = data.timestamp
    write_state()
    return {"success": True}


@app.get("/api/state")
async def get_state():
    return state

@app.post("/api/usd")
async def set_usd(alert: AlertValue):
    if alert.value <= 0:
        raise HTTPException(status_code=400, detail="USD amount must be positive")
    state["usd_amount"] = alert.value
    state["latest_prices"] = []  # Clear chart 🧹
    write_config()
    write_state()  # ✅ To persist wipe
    return {"success": True}


@app.post("/api/buy")
async def set_buy_alerts(alerts: AlertList):
    # Combine current alerts with new ones
    combined = set(state["buy_alerts"]) | set(alerts.values)
    state["buy_alerts"] = sorted(combined)
    write_config()
    return {"success": True}

@app.post("/api/sell")
async def set_sell_alerts(alerts: AlertList):
    # Combine current alerts with new ones
    combined = set(state["sell_alerts"]) | set(alerts.values)
    state["sell_alerts"] = sorted(combined)
    write_config()
    return {"success": True}

@app.delete("/api/buy")
async def delete_buy_alert(alert: AlertValue):
    value = round(alert.value, 8)
    if value in state["buy_alerts"]:
        state["buy_alerts"].remove(value)
        state["last_triggered_buy"].pop(f"{value:.8f}", None)
        write_config()
        write_state()
        return {"success": True}
    raise HTTPException(status_code=404, detail="Buy alert not found")

@app.delete("/api/sell")
async def delete_sell_alert(alert: AlertValue):
    value = round(alert.value, 8)
    if value in state["sell_alerts"]:
        state["sell_alerts"].remove(value)
        state["last_triggered_sell"].pop(f"{value:.8f}", None)
        write_config()
        write_state()
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
            return {"success": True}
        raise HTTPException(status_code=404, detail="Buy alert not found")
    elif data.side == "sell":
        if key in [f"{v:.8f}" for v in state["sell_alerts"]]:
            state["last_triggered_sell"].pop(key, None)
            write_state()
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
    existing = { normalize_rsi_key(e) for e in state["rsi_alerts"] }
    state["rsi_alerts"] = sorted(existing | new_keys)
    write_config()
    return {"success": True}

@app.delete("/api/rsi")
async def delete_rsi_alert(data: RsiDelete):
    # data.key is e.g. "above:40.00"
    if data.key in state["rsi_alerts"]:
        state["rsi_alerts"].remove(data.key)
        state["last_triggered_rsi"].pop(data.key, None)
        write_config()
        write_state()
        return {"success": True}
    raise HTTPException(status_code=404, detail="RSI alert not found")

@app.post("/api/rsi/reset-alert")
async def reset_rsi_alert(data: RsiReset):
     """
     Clears the last‐triggered timestamp for the exact RSI alert key.
     """
     if data.key in state["last_triggered_rsi"]:
         state["last_triggered_rsi"].pop(data.key)
         write_state()
         return {"success": True}
     raise HTTPException(status_code=404, detail="RSI alert not found")

@app.post("/api/rsi/interval")
async def set_rsi_interval(cfg: IntervalConfig):
    state["rsi_interval"] = cfg.interval
    write_config()
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
    return {"success": True}

@app.post("/api/price")
async def update_price(data: PriceData):
    state["latest_prices"].append(data.dict())
    state["latest_prices"] = state["latest_prices"][-100:]
    write_state()
    return {"success": True}



@app.get("/api/rsi")
async def get_rsi_status():
    # If you want to allow empty config, you can skip 400 here:
    SOLANATRACKER_API_KEY = os.getenv("SOLANATRACKER_API_KEY")
    if not SOLANATRACKER_API_KEY:
        return {
            "latest_rsi": None,
            "timestamp": None,
            "interval": state["rsi_interval"],
            "alerts": {},
            "reset_enabled": state["rsi_reset_enabled"],
        }

    # Fetch the actual RSI value (but don’t break the API if SolanaTracker is slow)
    try:
        rsi_value, rsi_time = get_latest_rsi(
            api_key=SOLANATRACKER_API_KEY,
            token=os.getenv("OUTPUT_MINT"),
            period=14,
            interval=state["rsi_interval"],
        )
    except Exception as e:
        # Log the timeout or other errors, then return “no data” rather than a 500
        print(f"⚠️ Failed to fetch RSI (continuing): {e}", flush=True)
        return {
            "latest_rsi": None,
            "timestamp": None,
            "interval": state["rsi_interval"],
            # build the alerts map exactly as below so UI still sees configured thresholds
            "alerts": {
                **{ key: { "triggered": True } for key in state["last_triggered_rsi"] },
                **{ key: { "triggered": False }
                     for key in state["rsi_alerts"]
                     if key not in state["last_triggered_rsi"] }
            },
            "reset_enabled": state["rsi_reset_enabled"],
        }

    # Build the alerts map from state["rsi_alerts"] (strings "above:30", etc.)
    RSI_STATE: dict[str, dict[str,bool]] = {}
    # first, mark anything already triggered
    for key in state["last_triggered_rsi"].keys():
        RSI_STATE[key] = {"triggered": True}
    # then ensure all configured alerts show up (untriggered if not in last_triggered_rsi)
    for entry in state["rsi_alerts"]:
        try:
            direction, val_str = entry.split(":", 1)
            val = float(val_str)
            key = f"{direction}:{val:.2f}"
        except Exception:
            continue
        if key not in RSI_STATE:
            RSI_STATE[key] = {"triggered": False}

    return {
        "latest_rsi":    round(rsi_value, 2),
        "timestamp":     rsi_time,
        "interval":      state["rsi_interval"],
        "alerts":        RSI_STATE,
        "reset_enabled": state["rsi_reset_enabled"],
    }



app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

@app.get("/{full_path:path}")
async def serve_index(full_path: str):
    index_path = os.path.join("frontend", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Page not found")



