from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import json
import os
from datetime import datetime

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
                "usd_amount": state["usd_amount"],
                "buy_alerts": state["buy_alerts"],
                "sell_alerts": state["sell_alerts"],
                "alert_reset_minutes": state["alert_reset_minutes"]
            }, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to write config.json: {e}")

def write_state():
    try:
        with open(STATE_PATH, "w") as f:
            json.dump({
                "latest_prices": state["latest_prices"],
                "last_triggered_buy": state["last_triggered_buy"],
                "last_triggered_sell": state["last_triggered_sell"]
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

class ResetAlert(BaseModel):
    side: str
    price: float

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
async def reset_single_alert(data: ResetAlert):
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

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

@app.get("/{full_path:path}")
async def serve_index(full_path: str):
    index_path = os.path.join("frontend", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Page not found")
