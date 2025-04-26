## 🚀 Jupiter USDC Price Alerts

A lightweight, Dockerized **price alert tool** using [Jupiter Aggregator](https://jup.ag) on Solana.  
Get real-time alerts when a token hits your custom **buy** or **sell** price targets using [ntfy.sh](https://ntfy.sh) push notifications.

---

🪙 **USDC-based tracking** for any Solana token  
💬 Alerts delivered to your **phone, browser, or desktop**  
🛠️ No signups required — fully open-source

---

### ✨ Features

- 🔄 Simulates USDC → Token and Token → USDC swaps
- 💥 Price includes price impact — see real trade values, not just spot prices  
- 🔔 Custom buy/sell price alerts  
- 📲 ntfy.sh push notifications  
- 🌐 Timezone-aware logs  
- 🔁 Auto-restart & log rotation  
- ⚡ Lightweight image (built with `python:3.12-slim`)

---

Link to Docker image: https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts

### 🧪 Docker Compose Example

Paste this into a `docker-compose.yml` file.

Update only the `OUTPUT_MINT` value with the token you want alerts for (e.g. BONK, JIM, PEPE).

```
version: '3.9'

services:
  jupiter-usdc-price-alert:
    image: nicxx2/jupiter-usdc-price-alerts:latest
    container_name: jupiter-usdc-price-alerts
    restart: unless-stopped

    environment:
      # ✅ TOKEN CONFIGURATION

      # INPUT_MINT: Always USDC for dollar-based pricing
      INPUT_MINT: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v

      # OUTPUT_MINT: The token you want to monitor (REPLACE this!)
      OUTPUT_MINT: <YOUR_OUTPUT_TOKEN_MINT>

      # ✅ AMOUNT TO SIMULATE (in USD)
      USD_AMOUNT: 100

      # ✅ CHECK INTERVAL (in seconds)
      CHECK_INTERVAL: 60

      # ✅ ALERT TRIGGERS
      BUY_ALERTS: "0.00135,0.00130"
      SELL_ALERTS: "0.00145,0.00150"

      # ✅ PUSH NOTIFICATIONS
      NTFY_TOPIC: token-alerts
      NTFY_SERVER: https://ntfy.sh

      # ✅ REPEAT ALERTS AFTER X MINUTES (0 to disable)
      ALERT_RESET_MINUTES: 0

      # ✅ LOCAL TIMEZONE
      TZ: Europe/London

    logging:
      driver: "json-file"
      options:
        max-size: "2m" # Max size per log file (2MB)
        max-file: "5" # Keep up to 5 rotated logs (~10MB total logs)
```

---

### 📲 Getting Started with Alerts

This tool sends notifications using [ntfy.sh](https://ntfy.sh), a free, open-source push service.

You can receive alerts **on your phone or browser** without creating any accounts.

#### Option 1: 📱 Mobile App
- [Android App](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
- [iOS App](https://apps.apple.com/us/app/ntfy/id1625396347)

Just open the app and **subscribe to your topic** (e.g. `token-alerts`)

#### Option 2: 🌐 Browser Alerts
- Go to: `https://ntfy.sh/<your-topic>`
- Example: `https://ntfy.sh/token-alerts`
- Enable browser notifications when prompted


---

### ✅ Supported Platforms

- 🖥️ `linux/amd64`  
- 🍓 `linux/arm64` (e.g. Raspberry Pi 4/5)  
- 🔧 `linux/arm/v7` (e.g. Raspberry Pi 3)

---

### 🧠 Tips for Beginners

- 💡 Token mint addresses can be found on [jup.ag](https://jup.ag) or [solscan.io](https://solscan.io)
- 🔒 Your `NTFY_TOPIC` is your alert channel — make it unique
- 📉 You can monitor any token priced in USDC
- 🧼 Log rotation is built-in (2MB, 5 files max)
