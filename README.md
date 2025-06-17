# nicxx2/jupiter-usdc-price-alerts

![Sponsored OSS](https://img.shields.io/badge/Sponsored-OSS-8a3af8?logo=github-sponsors&logoColor=white)

[![Docker Image Version](https://img.shields.io/docker/v/nicxx2/jupiter-usdc-price-alerts?label=version)](https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts)

[![Docker Pulls](https://img.shields.io/docker/pulls/nicxx2/jupiter-usdc-price-alerts)](https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts)

[![License](https://img.shields.io/github/license/nicxx2/jupiter-usdc-price-alerts)](LICENSE)



---

## 💖 Support This Project

If you found this helpful and want to support what I do, you can leave a tip here — thank you so much!

[![Support on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/nicxx2)

---


# 🚀 Jupiter USDC Price Alerts v2.2.2

A real-time, web-enabled price alert tool for Solana tokens using the **Jupiter Aggregator**.

Track simulated USDC swaps with real price impact and receive instant alerts via [ntfy.sh](https://ntfy.sh) — now with a **modern Web UI**!


---

## ✨ What's New in v2.2.2

### 📱 Mobile Layout Tweaks
- Improved layout so the UI now looks better on phones and smaller screens.

### 💼 Wallet Information Panel
- You can now add wallets to view real-time holdings and cost basis.
- A new **“All”** option shows the combined totals across all wallets.
- Requires: `SOLANATRACKER_API_KEY` and that you set `WALLET_ADDRESSES` with at least one wallet address.

---

## v2.1

### 🔍 RSI Indicators (Optional)
By providing a `SOLANATRACKER_API_KEY`, you enable a new RSI panel in the UI that lets you:

- **RSI is calculated on the token’s USD (USDC) price**, not on the token/SOL pair
- View live RSI values across multiple intervals: `1s`, `1m`, `5m`, `15m`, `1h`, `4h`
- Set **RSI alert thresholds**, e.g. `"below:30"`, `"above:70"`
- Optionally **auto-reset alerts** when RSI crosses back (toggle with `RSI_RESET_ENABLED`)


> 📝 To use RSI features, create a free account at [solanatracker.io](https://www.solanatracker.io/) and generate an API key  
> 🚦 Free API keys include **10,000 requests per month**

### 🛡️ Resilience to API Hiccups
- Handles rate limits (`429`) and timeouts gracefully
- If RSI data is unavailable, the UI displays `"––"` instead of crashing
- RSI alerts automatically resume on the next scheduled check

### 🖥️ RSI Alert Status Badges
- New badges show live RSI alert state:
  - 🟢 **Active**
  - 🟡 **Waiting to Reactivate**
  - 🔴 **Inactive**



---

- 🌐 **Live Web UI** — View current swap prices, price history, and your alert thresholds  
- 🧠 **On-the-fly updates** — Adjust USD amount, buy/sell targets, and more  
- 📈 **Chart View** — Visualize price trends and alert triggers over time  
- 🐳 **Single container build** — Backend + Web + Alert engine bundled together   

---

## 🔗 Docker Hub Repository

👉 [https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts](https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts)

---

## 🐳 Docker Compose Example

Paste the following into a `docker-compose.yml` file.

✅ Update the `OUTPUT_MINT` to the token you want to monitor (e.g. BONK, JIM, PEPE).  
🕒 Make sure to change the `TZ` (timezone) to match **your region** — this helps timestamps and cooldown logic align properly.

```yaml

services:
  jupiter-usdc-price-alert:
    image: nicxx2/jupiter-usdc-price-alerts:latest
    container_name: jupiter-usdc-price-alerts
    restart: unless-stopped

    # Expose the FastAPI backend for the Web UI
    ports:
      - "8000:8000"

    environment:
      # --- Token Configuration ---

      # Input mint (must be USDC for accurate USD-based alerts)
      INPUT_MINT: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v

      # Output token mint (token you want to monitor, e.g. JIM)
      OUTPUT_MINT: <YOUR_OUTPUT_TOKEN_MINT>

      # Solanatracker.io API key (required for RSI features)
      # If omitted, RSI data and alerts will be disabled in the UI
      SOLANATRACKER_API_KEY: ""

      # --- Simulated Swap Settings ---

      # Amount of USDC to simulate swapping
      USD_AMOUNT: 100

      # How often to check prices (in seconds)
      CHECK_INTERVAL: 60

      # --- Alert Triggers ---

      # Trigger buy alert if price is less than or equal to one of these
      BUY_ALERTS: "0.00138, 0.00135, 0.00136"

      # Trigger sell alert if price is greater than or equal to one of these
      SELL_ALERTS: "0.00140, 0.00145"

      # --- RSI Configuration ---

      # How often (in minutes) to refresh RSI checks
      # Note: Free solanatracker.io API keys are limited to 10,000 requests per month
      RSI_CHECK_INTERVAL: 5

      # RSI alert thresholds (e.g. "above:30", "below:70")
      RSI_ALERTS: "below:30"

      # Candle interval used for RSI calculation (e.g. 1s, 1m, 5m)
      RSI_INTERVAL: "5m"

      # If false, RSI alerts trigger only once per session
      RSI_RESET_ENABLED: "false"

      # Persisted, wallet tracking config
      # Comma-separated list of Solana wallet addresses to track
      # Note: Free solanatracker.io API keys are limited to 10,000 requests per month
      WALLET_ADDRESSES: ""

      # --- Push Notifications (via ntfy) ---

      # Unique topic name to receive notifications
      NTFY_TOPIC: token-alerts

      # Ntfy server URL (default: https://ntfy.sh)
      NTFY_SERVER: https://ntfy.sh

      # --- Alert Reset Cooldown ---

      # Minutes before the same buy/sell alert can trigger again (set to 0 to disable)
      ALERT_RESET_MINUTES: 0

      # --- Timezone ---

      # Local timezone for timestamps and scheduling
      TZ: Europe/London

    # --- Log Rotation ---

    logging:
      driver: "json-file"
      options:
        max-size: "2m"
        max-file: "5"

```

---
## 🌐 Accessing the Web UI

Once the container is running, you can view and control everything from a clean browser interface.

### ✅ How to Access

If you're running this locally:

`http://localhost:8000`



If running on a remote server, replace `localhost` with the IP address or hostname of your server:

`http://<your-server-ip>:8000`



You’ll be able to:

- View real-time buy/sell prices
- Add/remove alert thresholds on the fly
- Change the simulated USD amount
- See when each alert was triggered
- Watch charted price history with trigger lines
- (Optional) Set the SOLANATRACKER_API_KEY env var to enable a live 14-period USD-based RSI panel and “above:/below:” threshold alerts in the UI. If you omit the key, the RSI cards stay disabled (showing “—”).
- (Optional) View token holdings and cost basis for one or more wallets (with optional aggregation) using the SOLANATRACKER_API_KEY — requires setting `WALLET_ADDRESSES` with at least one wallet address. 



Web UI Example:

![Web UI Screenshot](https://github.com/Nicxx2/jupiter-usdc-price-alerts/blob/main/Jupiter_USDC_Price_Alert_Web_UI_with_RSI.png?raw=true)


Example of Wallet Information:

![Wallet Information Screenshot](https://github.com/Nicxx2/jupiter-usdc-price-alerts/blob/main/preview-wallet-ui-v2.2.1.png?raw=true)


---
## 📲 Push Alerts with `ntfy.sh`

This project uses [ntfy.sh](https://ntfy.sh) to send **free push notifications** to your browser or mobile device.

✅ No signup required  
✅ Works on Android, iOS, browsers, and terminals

> ⚠️ **Free Tier Note**: ntfy.sh allows up to **250 messages per IP address per day**.  
> If needed, you can **self-host** your own ntfy server and change the `NTFY_SERVER` variable in the Docker Compose file to point to your self-hosted instance (e.g. `http://localhost:8080`).

### ✅ How to Receive Alerts

**📱 Option 1: Mobile App**
- [Android App](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
- [iOS App](https://apps.apple.com/us/app/ntfy/id1625396347)

Open the app and **subscribe to your topic** (e.g. `token-alerts`).

---

**🌐 Option 2: Browser Alerts**
- Go to: `https://ntfy.sh/<your-topic>`
- Example: `https://ntfy.sh/token-alerts`
- Click “Allow” when your browser asks for notification permissions.

---

## 🧠 Tips for Beginners

- 💡 Token mint addresses can be found on [jup.ag](https://jup.ag) or [solscan.io](https://solscan.io)  
- 🔐 Your `NTFY_TOPIC` is your personal alert channel — make it unique  
- 📉 You can monitor any token priced in USDC with simulated slippage  
- 🧼 Log rotation is built-in (2MB, up to 5 files)

---

## ✅ Supported Platforms

- 🖥️ `linux/amd64`  
- 🍓 `linux/arm64` (Raspberry Pi 4/5)  
- 🧲 `linux/arm/v7` (Raspberry Pi 3 and older ARM chips)

---
