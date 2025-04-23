import os
import time
import requests
from datetime import datetime, timedelta, timezone

# Load config
INPUT_MINT = os.getenv("INPUT_MINT")
OUTPUT_MINT = os.getenv("OUTPUT_MINT")
USD_AMOUNT = float(os.getenv("USD_AMOUNT", "100"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
ALERT_RESET_MINUTES = int(os.getenv("ALERT_RESET_MINUTES", "0"))

print("✅ Starting script, checking env vars...", flush=True)
print(f"INPUT_MINT: {INPUT_MINT}", flush=True)
print(f"OUTPUT_MINT: {OUTPUT_MINT}", flush=True)

if not INPUT_MINT or not OUTPUT_MINT:
    print("❌ Missing required environment variables. Exiting.", flush=True)
    exit(1)

# Alerts
BUY_ALERTS = os.getenv("BUY_ALERTS", "").split(",")
SELL_ALERTS = os.getenv("SELL_ALERTS", "").split(",")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# Convert USD to lamports (6 decimals for USDC)
usdc_lamports = int(USD_AMOUNT * 1_000_000)

# Track last alert times (UTC-based)
last_buy_alert = {}
last_sell_alert = {}

def send_alert(title, message):
    if not NTFY_TOPIC:
        return
    try:
        url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip()
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title": safe_title,
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
    except Exception as e:
        print(f"❌ Failed to send alert: {e}", flush=True)

def get_out_amount(input_mint, output_mint, amount_lamports):
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippage=1"
    res = requests.get(url)
    if res.status_code == 200:
        data = res.json()
        return int(data.get("outAmount", 0)) / 1_000_000
    return None

def should_alert(alert_dict, key):
    now_utc = datetime.now(timezone.utc)
    last_time = alert_dict.get(key)
    if ALERT_RESET_MINUTES == 0:
        if key not in alert_dict:
            alert_dict[key] = now_utc
            return True
        return False
    if not last_time or (now_utc - last_time) >= timedelta(minutes=ALERT_RESET_MINUTES):
        alert_dict[key] = now_utc
        return True
    return False

def check_prices():
    print("\n" + "-" * 50, flush=True)  # Separator line

    # Show local time (will reflect DST & system timezone)
    local_now = datetime.now().astimezone()
    timestamp = local_now.strftime('%Y-%m-%d %H:%M:%S %Z')
    print(f"📅 {timestamp} — Price Check", flush=True)
    print("=" * (len(timestamp) + 22), flush=True)

    token_received = get_out_amount(INPUT_MINT, OUTPUT_MINT, usdc_lamports)
    usdc_returned = get_out_amount(OUTPUT_MINT, INPUT_MINT, int(token_received * 1_000_000)) if token_received else None

    if token_received:
        price_per_token_buy = USD_AMOUNT / token_received
        print(f"💵 Buying token with ${USD_AMOUNT} USDC:", flush=True)
        print(f"   Price per token: ${price_per_token_buy:.8f}", flush=True)
        print(f"   Token received: {token_received:.8f}", flush=True)

        for target in BUY_ALERTS:
            try:
                alert_price = float(target.strip())
                price_key = f"BUY-{alert_price:.5f}"
                if price_per_token_buy <= alert_price and should_alert(last_buy_alert, price_key):
                    send_alert("Buy Price Alert", f"Buy price ${price_per_token_buy:.8f} is ≤ target ${alert_price}")
            except ValueError:
                continue
    else:
        print("❌ Could not fetch USDC → token quote.", flush=True)

    if usdc_returned:
        price_per_token_sell = usdc_returned / token_received
        print(f"\n💸 Selling ${USD_AMOUNT} worth of token:", flush=True)
        print(f"   Price per token: ${price_per_token_sell:.8f}", flush=True)
        print(f"   USDC received: {usdc_returned:.8f}", flush=True)

        for target in SELL_ALERTS:
            try:
                alert_price = float(target.strip())
                price_key = f"SELL-{alert_price:.5f}"
                if price_per_token_sell >= alert_price and should_alert(last_sell_alert, price_key):
                    send_alert("Sell Price Alert", f"Sell price ${price_per_token_sell:.8f} is ≥ target ${alert_price}")
            except ValueError:
                continue
    else:
        print("❌ Could not fetch token → USDC quote.", flush=True)

if __name__ == "__main__":
    print("🚀 Jupiter Price Monitor started.", flush=True)
    while True:
        try:
            check_prices()
        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)
