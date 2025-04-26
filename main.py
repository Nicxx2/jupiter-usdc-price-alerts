import os
import time
import threading
import requests
import json
from datetime import datetime, timedelta, timezone

INPUT_MINT = os.getenv("INPUT_MINT")
OUTPUT_MINT = os.getenv("OUTPUT_MINT")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

shared_json_path = "/shared/jupiter-latest.json"
config_json_path = "/shared/config.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

USD_AMOUNT = float(os.getenv("USD_AMOUNT", 100.0))
BUY_ALERTS = []
SELL_ALERTS = []
ALERT_RESET_MINUTES = int(os.getenv("ALERT_RESET_MINUTES", 0))

last_buy_alert = {}
last_sell_alert = {}

print("✅ Starting script, checking env vars...", flush=True)
print(f"INPUT_MINT: {INPUT_MINT}", flush=True)
print(f"OUTPUT_MINT: {OUTPUT_MINT}", flush=True)

if not INPUT_MINT or not OUTPUT_MINT:
    print("❌ Missing required environment variables. Exiting.", flush=True)
    exit(1)

def parse_env_alerts(env_value):
    try:
        return [float(v.strip()) for v in env_value.split(",") if v.strip()]
    except Exception:
        return []

def load_dynamic_config():
    global USD_AMOUNT, BUY_ALERTS, SELL_ALERTS, ALERT_RESET_MINUTES

    # ——————— load config.json as before ———————
    if os.path.exists(config_json_path):
        try:
            with open(config_json_path) as f:
                data = json.load(f)
                USD_AMOUNT = float(data.get("usd_amount", USD_AMOUNT))
                BUY_ALERTS = data.get("buy_alerts", BUY_ALERTS)
                SELL_ALERTS = data.get("sell_alerts", SELL_ALERTS)
                ALERT_RESET_MINUTES = int(data.get("alert_reset_minutes", ALERT_RESET_MINUTES))
        except Exception as e:
            print(f"⚠️ Failed to load config.json: {e}", flush=True)
    else:
        print("ℹ️ No config.json found — using ENV defaults", flush=True)
        BUY_ALERTS = parse_env_alerts(os.getenv("BUY_ALERTS", ""))
        SELL_ALERTS = parse_env_alerts(os.getenv("SELL_ALERTS", ""))
        ALERT_RESET_MINUTES = int(os.getenv("ALERT_RESET_MINUTES", ALERT_RESET_MINUTES))

    # ——————— load & normalize jupiter-latest.json timestamps ———————
    if os.path.exists(shared_json_path):
        try:
            with open(shared_json_path) as f:
                state_data = json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to open jupiter-latest.json: {e}", flush=True)
            return

        local_tz = datetime.now().astimezone().tzinfo

        # rebuild last_buy_alert in UTC
        last_buy_alert.clear()
        for k, v in state_data.get("last_triggered_buy", {}).items():
            if not v:
                continue
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            last_buy_alert[k] = dt

        # rebuild last_sell_alert in UTC
        last_sell_alert.clear()
        for k, v in state_data.get("last_triggered_sell", {}).items():
            if not v:
                continue
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            last_sell_alert[k] = dt

    # ——————— prune any timestamps for alerts that no longer exist ———————
    valid_buy_keys  = { f"{float(x):.8f}" for x in BUY_ALERTS }
    for k in list(last_buy_alert):
        if k not in valid_buy_keys:
            last_buy_alert.pop(k)

    valid_sell_keys = { f"{float(x):.8f}" for x in SELL_ALERTS }
    for k in list(last_sell_alert):
        if k not in valid_sell_keys:
            last_sell_alert.pop(k)

    # now last_buy_alert & last_sell_alert only contain timestamps
    # for alerts still present in BUY_ALERTS / SELL_ALERTS




def to_lamports(amount): return int(amount * 1_000_000)

def send_alert(title, message):
    if not NTFY_TOPIC:
        return
    try:
        url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Content-Type": "text/plain; charset=utf-8"}
        )
    except Exception as e:
        print(f"❌ Failed to send alert: {e}", flush=True)

def notify_backend_trigger(side: str, price: float):
    try:
        requests.post("http://127.0.0.1:8000/api/trigger", json={
            "side": side,
            "price": round(price, 8),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        print(f"⚠️ Failed to notify backend of {side} trigger: {e}", flush=True)

def get_out_amount(input_mint, output_mint, amount_lamports):
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippage=1"
    res = requests.get(url)
    if res.status_code == 200:
        data = res.json()
        return int(data.get("outAmount", 0)) / 1_000_000
    return None


def should_alert(alert_dict, key):
    """
    Decide whether we should fire an alert for `key`, and return
    (allow: bool, timestamp_to_set: datetime or None).

    - If ALERT_RESET_MINUTES == 0: only allow on first encounter (when key not in alert_dict).
      Once triggered, it will remain blocked until you call reset (which removes alert_dict[key]).
    - If ALERT_RESET_MINUTES > 0: allow when there's no timestamp or the cooldown has expired.
    """
    now_utc = datetime.now(timezone.utc)
    last_time = alert_dict.get(key)

    # 🛑 Zero-reset mode: fire exactly once then block forever until manual reset
    if ALERT_RESET_MINUTES == 0:
        if last_time is None:
            return True, now_utc    # first trigger
        else:
            return False, None      # already triggered, stay off

    # From here on ALERT_RESET_MINUTES > 0

    # Normalize older, naive timestamps to UTC
    if last_time and last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=timezone.utc)

    # ✅ No previous trigger or cooldown expired → allow and clear old timestamp
    if not last_time or (now_utc - last_time) >= timedelta(minutes=ALERT_RESET_MINUTES):
        if last_time:
            alert_dict.pop(key, None)
        return True, now_utc

    # ❌ Still in cooldown
    return False, None




def write_status_json(price_buy, price_sell, token_received, usdc_returned):
    try:
        json_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "usd_amount": USD_AMOUNT,
            "price_per_token_buy": round(price_buy, 8) if price_buy else None,
            "price_per_token_sell": round(price_sell, 8) if price_sell else None,
            "token_received": round(token_received, 8) if token_received else None,
            "usdc_returned": round(usdc_returned, 8) if usdc_returned else None,
            "buy_alerts": BUY_ALERTS,
            "sell_alerts": SELL_ALERTS,
            "last_triggered_buy": {k: v.isoformat() for k, v in last_buy_alert.items()},
            "last_triggered_sell": {k: v.isoformat() for k, v in last_sell_alert.items()},
            "alert_reset_minutes": ALERT_RESET_MINUTES
        }
        with open(shared_json_path, "w") as f:
            json.dump(json_data, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to write shared status file: {e}", flush=True)

def check_prices():
    load_dynamic_config()
    usdc_lamports = to_lamports(USD_AMOUNT)

    local_now = datetime.now().astimezone()
    print(f"\n📅 {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')} — Price Check", flush=True)

    # ✅ Clear expired cooldowns so alerts behave like fresh ones
    now_utc = datetime.now(timezone.utc)
    if ALERT_RESET_MINUTES > 0:
        cooldown_delta = timedelta(minutes=ALERT_RESET_MINUTES)

        # Clean up buy alerts
        for key in list(last_buy_alert.keys()):
            last_time = last_buy_alert[key]
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if (now_utc - last_time) >= cooldown_delta:
                print(f"🔁 Cooldown expired — clearing BUY alert {key}", flush=True)
                del last_buy_alert[key]

        # Clean up sell alerts
        for key in list(last_sell_alert.keys()):
            last_time = last_sell_alert[key]
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if (now_utc - last_time) >= cooldown_delta:
                print(f"🔁 Cooldown expired — clearing SELL alert {key}", flush=True)
                del last_sell_alert[key]

    # ✅ Force timestamp cleanup if alert is marked Active again
    all_buy_keys = [f"{float(b):.8f}" for b in BUY_ALERTS]
    all_sell_keys = [f"{float(s):.8f}" for s in SELL_ALERTS]

    for key in all_buy_keys:
        ready, _ = should_alert(last_buy_alert, key)
        if ready and key in last_buy_alert:
            print(f"🧹 Auto-clean: BUY alert {key} is active — clearing old timestamp", flush=True)
            del last_buy_alert[key]

    for key in all_sell_keys:
        ready, _ = should_alert(last_sell_alert, key)
        if ready and key in last_sell_alert:
            print(f"🧹 Auto-clean: SELL alert {key} is active — clearing old timestamp", flush=True)
            del last_sell_alert[key]

    # ✅ Fetch price data
    token_received = get_out_amount(INPUT_MINT, OUTPUT_MINT, usdc_lamports)
    usdc_returned = get_out_amount(OUTPUT_MINT, INPUT_MINT, to_lamports(token_received)) if token_received else None

    price_buy = price_sell = None

    # ✅ BUY CHECK
    if token_received:
        price_buy = USD_AMOUNT / token_received
        print(f"💵 Buying token with ${USD_AMOUNT} USDC:")
        print(f"   Price per token: ${price_buy:.8f}")
        print(f"   Token received: {token_received:.8f}")

        for target in BUY_ALERTS:
            try:
                alert_price = float(str(target).strip())
                price_key = f"{alert_price:.8f}"
                trigger_ready, trigger_time = should_alert(last_buy_alert, price_key)

                if trigger_ready and price_buy <= alert_price:
                    send_alert("Buy Price Alert", f"Buy price ${price_buy:.8f} is ≤ target ${alert_price}")
                    notify_backend_trigger("buy", alert_price)
                    last_buy_alert[price_key] = trigger_time
                    write_status_json(price_buy, price_sell, token_received, usdc_returned)
            except ValueError:
                continue
    else:
        print("❌ Could not fetch USDC → token quote.", flush=True)

    # ✅ SELL CHECK
    if usdc_returned and token_received:
        price_sell = usdc_returned / token_received
        print(f"\n💸 Selling ${USD_AMOUNT} worth of token:")
        print(f"   Price per token: ${price_sell:.8f}")
        print(f"   USDC received: {usdc_returned:.8f}")

        for target in SELL_ALERTS:
            try:
                alert_price = float(str(target).strip())
                price_key = f"{alert_price:.8f}"
                trigger_ready, trigger_time = should_alert(last_sell_alert, price_key)

                if trigger_ready and price_sell >= alert_price:
                    send_alert("Sell Price Alert", f"Sell price ${price_sell:.8f} is ≥ target ${alert_price}")
                    notify_backend_trigger("sell", alert_price)
                    last_sell_alert[price_key] = trigger_time
                    write_status_json(price_buy, price_sell, token_received, usdc_returned)
            except ValueError:
                continue
    else:
        print("❌ Could not fetch token → USDC quote.", flush=True)

    # ✅ Final status save and debug tracking
    write_status_json(price_buy, price_sell, token_received, usdc_returned)
    print(f"🧠 Tracked BUY cooldowns: {list(last_buy_alert.keys())}", flush=True)
    print(f"🧠 Tracked SELL cooldowns: {list(last_sell_alert.keys())}", flush=True)

    try:
        requests.post("http://127.0.0.1:8000/api/price", json={
            "timestamp": datetime.now().isoformat(),
            "buy_price": price_buy,
            "sell_price": price_sell
        })
    except Exception as e:
        print(f"❌ Failed to send price to backend: {e}", flush=True)


def background_alert_cleaner():
    while True:
        # 🔄 pick up any UI changes (reset‐minutes or manual resets)
        load_dynamic_config()

        now_utc = datetime.now(timezone.utc)
        usdc_lamports = to_lamports(USD_AMOUNT)

        # fetch live buy/sell prices
        token_received = get_out_amount(INPUT_MINT, OUTPUT_MINT, usdc_lamports)
        usdc_returned = (
            get_out_amount(OUTPUT_MINT, INPUT_MINT, to_lamports(token_received))
            if token_received else None
        )
        price_buy = USD_AMOUNT / token_received if token_received else None
        price_sell = usdc_returned / token_received if token_received and usdc_returned else None

        for alert_list, alert_dict, current_price, label in [
            (BUY_ALERTS,  last_buy_alert,  price_buy,  "buy"),
            (SELL_ALERTS, last_sell_alert, price_sell, "sell")
        ]:
            for raw_price in alert_list:
                key = f"{float(raw_price):.8f}"
                last_time = alert_dict.get(key)
                if not last_time:
                    continue

                # ensure tz‐aware UTC
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                else:
                    last_time = last_time.astimezone(timezone.utc)

                delta = now_utc - last_time
                cooldown_expired = (
                    ALERT_RESET_MINUTES > 0 and
                    delta >= timedelta(minutes=ALERT_RESET_MINUTES)
                )
                should_be_active = (
                    current_price is not None and
                    ((label == "buy"  and current_price <= float(raw_price)) or
                     (label == "sell" and current_price >= float(raw_price)))
                )

                if cooldown_expired and should_be_active:
                    try:
                        print(f"🧹 [BG] {label.upper()} alert {key} expired — auto-resetting", flush=True)
                        resp = requests.post(
                            "http://127.0.0.1:8000/api/reset-alert",
                            json={"side": label, "price": float(raw_price)}
                        )
                        if resp.ok:
                            # clear locally and persist so check_prices/UI see it immediately
                            alert_dict.pop(key, None)
                            write_status_json(None, None, None, None)
                    except Exception as e:
                        print(f"❌ [BG] Failed to auto-reset {label.upper()} alert {key}: {e}", flush=True)

        time.sleep(5)





# 🚀 NEW: Handle reset requests that trigger again immediately if needed
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

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

    # ✨ Immediately write updated config so it's saved
    write_status_json(None, None, None, None)
    return {"success": True}

if __name__ == "__main__":
    print("🚀 Jupiter Price Monitor started.", flush=True)
    
    # 🧠 Start background cleaner in a thread
    threading.Thread(target=background_alert_cleaner, daemon=True).start()
    
    
    while True:
        try:
            check_prices()
        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)
