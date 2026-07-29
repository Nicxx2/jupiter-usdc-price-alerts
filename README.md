![Sponsored OSS](https://img.shields.io/badge/Sponsored-OSS-8a3af8?logo=github-sponsors&logoColor=white)

![Docker Pulls](https://img.shields.io/docker/pulls/nicxx2/jupiter-usdc-price-alerts)

![License](https://img.shields.io/github/license/Nicxx2/jupiter-usdc-price-alerts)


---
## 💖 Support This Project

If you found this helpful and want to support what I do, you can leave a tip here — thank you so much!

[![Support on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/nicxx2)

---

# 🚀 Jupiter USDC Price Alerts v3.3.5

A real-time, web-enabled price alert tool for Solana tokens using the **Jupiter Aggregator**.

Track simulated USDC swaps with real price impact across one or many Solana tokens, then receive instant alerts via [ntfy.sh](https://ntfy.sh) from a **modern Web UI**.

---

## ✨ What's New in v3.3.5

- Added safer confirmation for unusually large Jupiter price moves before updating charts, shared quote caches, or firing alerts.
- Improved multi-token monitoring with live Token Overview updates, a clear active-token marker, accurate overdue scheduling, independent background alerts, safer notification retries, and cleaner fired-alert timing details.

### 📈 Per-rule Action Readiness trends
- Open the dedicated graph button on any enabled rule card to switch safely between its live value and its history.
- Choose **24h, 7d, 30d, or 90d** views on desktop or mobile; each coin and rule remembers its own view and range in that browser.
- Actual readings are plotted against the rule target, including stepped target changes and separate price-impact scenario segments.

### 🛡️ Truthful gaps and provider timing
- Rule Trends reuse completed Action Readiness evaluations and make **no additional Jupiter requests**.
- Only fresh, finite readings are saved. Unknown, stale, malformed, disabled, and interrupted periods appear as gaps and are never converted to zero.
- Jupiter token metrics are deduplicated by the provider's source timestamp, while live price-impact quotes use their own quote timestamp.

### 💾 Bounded, isolated history
- Trend history is stored in `/shared/action-rules-history.sqlite3`, separately from current readiness, configuration, alerts, and price history.
- Recent readings stay detailed, older readings are progressively downsampled, and retention is bounded to 90 days by default.
- History endpoints return a mobile-safe bounded number of points, and deleting a tracked coin also deletes its rule history.
- A history storage problem cannot stop current Action Readiness checks or alerts.

### 📱 Focused responsive cards
- A dedicated 36px graph control prevents accidental card activation and keeps the current Pass / Not passed / Unknown status visible.
- Charts use compact labels and tap-friendly tooltips, disable animation for motion safety, and explain gaps and the latest valid reading.

---

## ✨ What's New in v3.2

### ✅ Action Rules and Action Readiness
- Create independent per-coin conditions for holder count, market cap, liquidity, 24-hour volume, sell pressure, volume/liquidity ratio, and maximum Jupiter sell-quote price impact.
- See each rule's **current value**, **target**, and **Pass / Not passed / Unknown** result in a responsive Action Readiness card.
- Enable or disable Action Rules globally or per coin while preserving every coin's saved configuration.
- Jupiter-reported holder counts are clearly identified as provider data and may differ slightly from other indexers.

### 🔔 Confirmed Action Rules alerts
- Optionally send an ntfy alert when every enabled rule has passed two fresh checks.
- Choose a **send once** alert or **re-arm** after two confirmed failures followed by two new passes.
- Stale, missing, malformed, or zero-denominator data stays **Unknown** and cannot trigger an alert.
- Per-coin ntfy topics override the global topic, and delivery failures retry on later checks without repeating a successfully completed one-time alert.

### 🔌 Safe Jupiter integration
- The optional `JUPITER_API_KEY` is used only by Action Rules for Tokens V2 data and authenticated sell quotes.
- Without that key, Action Rules remain unavailable while the existing keyless Jupiter price monitor and price alerts continue unchanged.
- Action Rules use a separate conservative limiter, batch metadata for up to 100 mints, and retry only transient failures such as timeouts, `429`, and server errors.
- Permanent `4xx` errors stop only the current retry burst; the next scheduled refresh still tries again normally.
- Signed Swap V2 price impact is normalized into adverse percentage points so a negative signed value cannot incorrectly pass a maximum-impact rule.

### 📱 UI and long-running reliability
- Action Readiness starts as a compact, expandable status bar, remembers each coin's browser expand/collapse preference, and stays responsive on desktop and mobile.
- When `JUPITER_API_KEY` is blank or Action Rules are globally disabled, Action Readiness and Rules indicators stay hidden from the main dashboard; Settings clearly explains the inactive state. A global visibility change only takes effect after Settings is saved.
- Settings displays the effective Action Rules refresh interval and isolated limiter from Docker/ENV configuration.
- Global/per-coin transitions, restarts, stale gaps, unrelated token edits, and alert state persistence are handled without false-ready alerts.
- Re-enabling RSI now shows a waiting state immediately instead of briefly reporting that RSI is still disabled.

---

## 🧾 What's New in v3.1.1

### 🪙 Multi-token monitoring
- Track multiple output tokens from the web UI while keeping Docker Compose as the simple default setup.
- Validate new token mints with Jupiter before saving them.
- Switch the active token without losing each token's saved chart, alert, RSI, and wallet context.

### ⚙️ Token-isolated settings
- Each token can have its own simulated USD amount, buy/sell alerts, alert reset minutes, RSI alerts, RSI interval, RSI reset mode, decimals, price check interval, RSI check interval, and optional ntfy topic.
- Per-token ntfy test notifications make it easy to confirm the right topic before alerts fire.
- Chart window, wallet selection, and sell simulator preferences are remembered per token in the browser.

### 🔌 API and wallet reliability
- Jupiter price checks now use keyless Swap V2 quote-only requests with a dedicated 0.5 req/sec default limiter.
- Wallet info now uses SolanaTracker PnL V2 batch positions with a fallback to basic wallet holdings for migrated or not-yet-indexed tokens.
- Sell simulator keeps proceeds visible for holding-only data, but avoids showing a misleading principal/profit split until PnL is indexed.
- Long SolanaTracker RSI and wallet status errors wrap cleanly within mobile and desktop cards without changing the original error text.

### 🛡️ Safer rate and state management
- SolanaTracker calls use a shared safe limiter by default for free accounts, with custom/off modes for higher plans.
- Scheduler checks tokens in a conservative round-robin flow so multiple tokens queue cleanly instead of spiking API calls.
- SolanaTracker can be disabled globally from Settings, hiding RSI, wallet info, and sell simulator while Jupiter price alerts keep running.
- RSI can also be disabled per token when you only want SolanaTracker checks on selected tokens.
- Missing or failed RSI data displays as `--` with status context instead of a fake `0.00`.
- Price history and quote caches are scoped per token and cleared when calculation settings change.

---

## v2.4

### 🌙 Dark Mode Support
- **Persistent theme toggle** — Sun/moon button in top-right corner
- **System preference detection** — Automatically detects your OS dark/light preference on first visit
- **Per-device memory** — Each browser/device remembers your theme choice
- **Dark-aware charts** — Chart.js axes, grid, and tooltips adapt to dark mode
- **Improved dropdowns** — All select elements now have proper dark mode styling
- **Smooth transitions** — Clean theme switching without page reload

---

## v2.3.1

### 🔗 Jupiter API Update
- Migrated from the deprecated `quote-api.jup.ag/v6` to the new `lite-api.jup.ag/swap/v1`.
- Added retry and backoff logic for more resilient price checks.
- Added `restrictIntermediateTokens=true` to reduce rare dust-pool price spikes.

---

## v2.3

### 🧮 Sell % Simulator (Wallets)
Quickly model a partial exit from your position and see an estimated breakdown of proceeds:

- Adjust **0–100%** via slider, numeric input, or quick chips
- Shows:
  - Estimated **current token price**
  - **Tokens to sell**
  - **Expected proceeds**
  - Split into **principal** vs **unrealized profit/loss**
- Works with the **selected wallet** or **All** (aggregate)
- **Hidden** when no wallets are configured or when the selected source has no holdings
- Uses the latest values from the Wallet Info card (PnL):  
  `price ≈ current_value / holding`, with proceeds split using the current **cost basis** and **unrealized** PnL
- Note: This is an **estimate**; execution may vary due to price impact and slippage

---

## v2.2.2

### 📱 Mobile Layout Tweaks
- Improved layout so the UI now looks better on phones and smaller screens.

### 💼 Wallet Information Panel
- You can now add wallets to view real-time holdings and cost basis.
- A new **“All”** option shows the combined totals across all wallets.
- Requires: `SOLANATRACKER_API_KEY`; wallets can be set with `WALLET_ADDRESSES` or added from the Web UI.

---

## v2.1

### 🔍 RSI Indicators (Optional)
By providing a `SOLANATRACKER_API_KEY`, you enable a new RSI panel in the UI that lets you:

- **RSI is calculated on the token’s USD (USDC) price**, not on the token/SOL pair
- View live RSI values across multiple intervals: `1s`, `1m`, `5m`, `15m`, `1h`, `4h`
- Set **RSI alert thresholds**, e.g. `"below:30"`, `"above:70"`
- Optionally **auto-reset alerts** when RSI crosses back (toggle with `RSI_RESET_ENABLED`)


> 📝 To use RSI features, create a free account at [solanatracker.io](https://www.solanatracker.io/) and generate an API key  
> 🚦 Free API keys include **2,500 requests per month**

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

      # Solanatracker.io API key (required for RSI, wallet info, and sell simulator)
      # If omitted, SolanaTracker-only panels will be disabled in the UI
      SOLANATRACKER_API_KEY: ""

      # Master SolanaTracker feature switch. Docker is the first-run default;
      # saved app settings in /shared/config.json take priority after you edit them in the UI.
      SOLANATRACKER_ENABLED: "true"

      # SolanaTracker limiter: safe, custom, or off.
      # Safe mode is conservative for free accounts and leaves room for RSI/wallet calls.
      # Use custom/off only if your plan allows it.
      SOLANATRACKER_RATE_LIMIT_MODE: safe
      SOLANATRACKER_REQUESTS_PER_SECOND: 1

      # Optional and used only by Action Rules. Existing prices remain keyless.
      # Leave blank to keep Action Rules unavailable; create one at https://portal.jup.ag.
      JUPITER_API_KEY: ""

      # Existing keyless price-monitor limiter (unchanged by Action Rules).
      JUPITER_REQUESTS_PER_SECOND: 0.5

      # Separate authenticated limiter used only by Action Rules.
      COMMUNITY_RULES_JUPITER_REQUESTS_PER_SECOND: 0.5
      # This is forced off automatically while JUPITER_API_KEY is blank.
      COMMUNITY_RULES_ENABLED: "true"
      COMMUNITY_RULES_CHECK_INTERVAL: 120
      COMMUNITY_RULES_STALE_SECONDS: 360
      COMMUNITY_RULES_QUOTE_MAX_AGE_SECONDS: 90
      COMMUNITY_RULES_SOURCE_MAX_AGE_SECONDS: 3600
      # Rule Trends reuse Action Readiness results and add no Jupiter calls.
      COMMUNITY_RULES_HISTORY_PATH: /shared/action-rules-history.sqlite3
      COMMUNITY_RULES_HISTORY_RETENTION_DAYS: 90
      COMMUNITY_RULES_HISTORY_MAX_POINTS: 480

      # --- Simulated Swap Settings ---

      # Amount of USDC to simulate swapping
      USD_AMOUNT: 100

      # Optional token decimals override. Leave blank for auto-detect/fallback.
      # USDC is 6 decimals; output token decimals vary by mint.
      INPUT_DECIMALS: ""
      OUTPUT_DECIMALS: ""

      # How often to check prices (in seconds)
      CHECK_INTERVAL: 60

      # --- Alert Triggers ---

      # Trigger buy alert if price is less than or equal to one of these
      BUY_ALERTS: "0.00138, 0.00135, 0.00136"

      # Trigger sell alert if price is greater than or equal to one of these
      SELL_ALERTS: "0.00140, 0.00145"

      # --- RSI Configuration ---

      # Per-token RSI can also be switched off in the UI
      RSI_ENABLED: "true"

      # How often (in minutes) to refresh RSI checks
      # Note: Free solanatracker.io API keys are limited to 2,500 requests per month
      RSI_CHECK_INTERVAL: 5

      # RSI alert thresholds (e.g. "above:30", "below:70")
      RSI_ALERTS: "below:30"

      # Candle interval used for RSI calculation (e.g. 1s, 1m, 5m)
      RSI_INTERVAL: "5m"

      # If false, RSI alerts trigger only once per session
      RSI_RESET_ENABLED: "false"

      # Persisted, wallet tracking config
      # Comma-separated list of Solana wallet addresses to track
      # Note: Free solanatracker.io API keys are limited to 2,500 requests per month
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

    # Persist UI-edited settings, alert state, and price history
    volumes:
      - jupiter-alert-data:/shared

    # --- Log Rotation ---

    logging:
      driver: "json-file"
      options:
        max-size: "2m"
        max-file: "5"

volumes:
  jupiter-alert-data:

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

- View real-time buy/sell prices for the active token
- Add, validate, remove, and switch between multiple tracked tokens from the web UI
- Keep each token's simulated USD amount, buy/sell alerts, RSI alerts, RSI interval, RSI reset mode, RSI on/off preference, decimals, ntfy topic, and check intervals isolated
- With the optional feature-only `JUPITER_API_KEY`, configure independent per-coin Action Rules for holders, market cap, liquidity, 24h volume, sell pressure, volume/liquidity, and Jupiter sell-quote price impact, with pass/fail/unknown status and optional confirmed ntfy alerts
- Enable/disable SolanaTracker globally, choose safe/custom/off rate management, and see the RSI check interval plus monthly call estimate from settings
- See when each alert was triggered and reset individual alert cooldowns
- Watch token-scoped chart history with selectable 2h, 4h, 6h, 12h, and 24h windows
- Send test notifications for the global ntfy topic or a token-specific ntfy topic
- (Optional) Set the `SOLANATRACKER_API_KEY` env var to enable the cached 14-period USD-based RSI panel and `above:` / `below:` threshold alerts. If you omit the key, RSI stays disabled and shows `--`.
- (Optional) View token holdings and cost basis for one or more wallets, then use the sell simulator for the active token. This requires `SOLANATRACKER_API_KEY` and wallet addresses configured in Docker Compose or the web UI.



## 📸 See It in Action

> [!NOTE]
> Market values in the BONK, WIF, and JUP screenshots below are clearly labelled illustrative demo data. They are not live market data or financial advice.

### Multi-token monitoring

![Multi-token monitoring with BONK, WIF, and JUP demo scenarios](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/01-multi-token-overview.png)

### Action Readiness

![Confirmed Action Readiness rules for the BONK demo scenario](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/02-action-readiness-ready.png)

### Alerts and saved price history

![Price alerts and saved price history for the WIF demo scenario](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/07-price-alerts-and-chart.png)

<details>
<summary><strong>📷 Click here for more screenshots</strong></summary>

### Per-rule trends

Rule Trends reuse saved Action Readiness checks. Gaps remain visible instead of being plotted as zero.

![Thirty-day holder trend with a target line and an honest collection gap](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/03-rule-trend-30d.png)

### Risk thresholds

![A WIF demo scenario where sell pressure, liquidity ratio, and price impact block readiness](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/04-risk-thresholds.png)

### Rule builder

![Per-token Action Rules, alert behavior, targets, and price-impact amount](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/05-rule-builder.png)

### Settings and rate management

![Global Action Rules, isolated Jupiter limiter, SolanaTracker settings, and ntfy topic](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/06-settings-and-rate-limits.png)

### Confirmation protection

![All rules passing while Action Readiness waits for confirmation sample two of two](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/08-confirmation-protection.png)

### Mobile layout

![Compact mobile token and Action Readiness overview](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/09-mobile-overview.png)

![Thirty-day Rule Trend on mobile](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/10-mobile-rule-trend.png)

![Stacked Action Readiness rule cards on mobile](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/11-mobile-rule-cards.png)

![Responsive mobile settings panel](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/docs/screenshots/12-mobile-settings.png)

### Wallet tracking

![Wallet Information Screenshot](https://raw.githubusercontent.com/Nicxx2/jupiter-usdc-price-alerts/main/preview-wallet-ui-v2.2.1.png)

</details>


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

## v3.3.4 Production Notes

- Rule history begins with the first fresh evaluation after upgrading; the app does not invent or backfill older readings.
- `/shared/action-rules-history.sqlite3` is created automatically in the existing persistent volume. No existing JSON migration is required.
- Current readiness and alerts do not depend on history storage. SQLite write or read failures are isolated and reported without changing rule outcomes.
- Fresh recent points remain detailed. Points older than 7 days are compacted into 15-minute buckets, points older than 30 days into hourly buckets, and points older than the configured retention are deleted.
- Each history response is capped and downsampled for responsive mobile charts. Unknown and disabled periods remain explicit gaps.
- Price-impact trend segments include the configured tracked-USDC or custom-token scenario so different simulated sell amounts are not presented as one continuous series.

---

## v3.2 Production Notes

- Existing `/shared/config.json` and `jupiter-latest.json` data remain compatible. Internal `community_rules_*` keys are intentionally retained so upgrades do not lose saved settings.
- Add `JUPITER_API_KEY` to Docker Compose and restart the container before enabling Action Rules. The key is not added to existing keyless price requests.
- Action Rules default to a separate `0.5` requests/second limiter and a 120-second refresh interval. Missing or stale provider data fails safely to **Unknown**.
- The Action Readiness result is a market snapshot based on user-selected conditions, not financial advice or an execution guarantee.

---

## v3.1.1 Production Notes

- The web UI now has a settings button next to dark mode for runtime settings. Docker Compose values remain the defaults; UI edits are persisted into `/shared/config.json`.
- The web UI can save multiple output tokens, validate new token mints with Jupiter before adding them, and switch the active token without editing Docker Compose. Jupiter quotes use keyless Swap V2 quote-only requests, and the monitor rotates through enabled saved tokens with a conservative due-token scheduler while the active token still feeds the main chart and controls.
- Each saved token can have its own simulated USD amount, buy/sell alerts, RSI alerts, RSI interval, RSI reset mode, RSI on/off preference, decimals, optional `ntfy_topic`, price check interval, and RSI check interval. Blank cadence/topic values inherit the Docker Compose / global defaults.
- Token alert messages include the token label and mint, and the UI can send a per-token ntfy test notification so topics can be verified before alerts fire.
- Mount `/shared` as shown above so alerts, wallets, price history, and UI-edited settings survive container recreation.
- RSI is cached by the monitor process. The UI reads the cached value and status instead of making extra SolanaTracker candle calls per browser tab.
- Missing or failed RSI data is shown as `--` with a small status dot instead of `0.00`.
- `SOLANATRACKER_ENABLED=true` is the first-run default for SolanaTracker-only features. Turning it off in the UI persists to `/shared/config.json` and hides RSI, wallet info, and the sell simulator without stopping Jupiter price checks.
- `SOLANATRACKER_RATE_LIMIT_MODE=safe` protects free SolanaTracker accounts with a conservative 1 request/second limit. The UI labels this separately from the RSI check interval so request pacing is not confused with RSI cadence. SolanaTracker currently lists the Data API Free plan as 2,500 requests/month and 3 req/sec, so safe mode leaves headroom for RSI and wallet calls. Use `custom` with `SOLANATRACKER_REQUESTS_PER_SECOND`, or `off` only when your plan/private setup can handle it. See the [SolanaTracker pricing docs](https://docs.solanatracker.io/pricing) for current limits.
- Token decimals are auto-detected through Solana RPC when possible, with `INPUT_DECIMALS` / `OUTPUT_DECIMALS` available as explicit overrides.
- Quick offline validation: `python -m unittest tests.test_scheduler_rate_limit` checks scheduler interval inheritance, round-robin due-token selection, and rate-limit safe/custom/off behavior without live API calls.
