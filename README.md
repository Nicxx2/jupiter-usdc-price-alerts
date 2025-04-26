# 🚀 Jupiter USDC Price Alerts v2

A real-time, web-enabled price alert tool for Solana tokens using the **Jupiter Aggregator**.

Track simulated USDC swaps with real price impact and receive instant alerts via [ntfy.sh](https://ntfy.sh) — now with a **modern Web UI**!

---

## ✨ What's New in v2

- 🌐 **Live Web UI** — View current swap prices, price history, and your alert thresholds  
- 🧠 **On-the-fly updates** — Adjust USD amount, buy/sell targets, and view real logs  
- 📈 **Chart View** — Visualize price trends and alert triggers over time  
- 🐳 **Single container build** — Backend + Web + Alert engine bundled together  
- ⚙️ **Minimal config needed** — Just change the token mint address and you're ready  

---

## 🔗 Docker Hub Repository

👉 [https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts](https://hub.docker.com/r/nicxx2/jupiter-usdc-price-alerts)

---

## 🐳 Docker Compose Example

Paste the following into a `docker-compose.yml` file.

✅ Update the `OUTPUT_MINT` to the token you want to monitor (e.g. BONK, JIM, PEPE).  
🕒 Make sure to change the `TZ` (timezone) to match **your region** — this helps timestamps and cooldown logic align properly.

```yaml
version: '3.9'

services:
  jupiter-usdc-price-alert:
    image: nicxx2/jupiter-usdc-price-alerts:latest
    container_name: jupiter-usdc-price-alerts
    restart: unless-stopped

    ports:
      # Access the Web UI at http://localhost:8000
      - "8000:8000"

    environment:
      # --- TOKEN CONFIGURATION ---
      # Fixed: USDC mint address
      INPUT_MINT: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v

      # Replace with the token you want to monitor
      OUTPUT_MINT: <YOUR_OUTPUT_TOKEN_MINT>

      # --- SIMULATED SWAP AMOUNT ---
      # Amount of USDC to simulate each check
      USD_AMOUNT: 100

      # --- CHECK INTERVAL ---
      # How often to check prices (in seconds)
      CHECK_INTERVAL: 60

      # --- ALERT THRESHOLDS ---
      # Trigger alerts if price falls below these values
      BUY_ALERTS: "0.00135,0.00130"

      # Trigger alerts if price rises above these values
      SELL_ALERTS: "0.00145,0.00150"

      # --- PUSH NOTIFICATIONS ---
      # The topic name for ntfy notifications
      NTFY_TOPIC: token-alerts

      # Default ntfy server (250 messages/day/IP)
      NTFY_SERVER: https://ntfy.sh

      # --- COOLDOWN SETTINGS ---
      # 0 = fire once per target; set minutes to allow repeats
      ALERT_RESET_MINUTES: 0

      # --- LOCAL TIMEZONE ---
      # Update to match your timezone
      TZ: Europe/London

    logging:
      driver: "json-file"
      options:
        # Limit individual log files to 2MB
        max-size: "2m"
        # Keep 5 rotated log files (10MB total max)
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


Web UI Example:

![Web UI Screenshot](https://github.com/Nicxx2/jupiter-usdc-price-alerts/blob/main/Jupiter_USDC_Price_Alert_Web_UI.png)


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
