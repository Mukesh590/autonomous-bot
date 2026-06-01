# Setup Guide

## Prerequisites

- Python 3.12+
- An Alpaca account (free) with paper trading API keys
- A Railway account (free tier works) for deployment

---

## 1. Get Alpaca API Keys

1. Go to [app.alpaca.markets](https://app.alpaca.markets)
2. Sign up or log in
3. In the left sidebar, switch to **Paper Trading** (not live)
4. Click **API Keys** → **Generate New Key**
5. Copy the **API Key ID** and **Secret Key** — you won't see the secret again

---

## 2. Local Setup

```bash
# Clone / enter the project directory
cd autonomous-bot

# Create a virtual environment
python -m venv venv

# Activate it
# macOS/Linux:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
```

Edit `.env` and fill in your keys:
```
ALPACA_API_KEY=your_actual_key
ALPACA_SECRET_KEY=your_actual_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

---

## 3. Run Locally

```bash
python bot.py
```

The bot will:
- Connect to Alpaca and print your portfolio value
- If the market is open, run the first cycle immediately
- Log to `logs/bot.log`, `logs/signals.csv`, `logs/trades.csv`, `logs/portfolio.csv`
- Run every 15 minutes, 9:45–15:45 ET on weekdays
- Print nothing outside market hours except a next-open message

To run outside market hours (for testing), you can temporarily change `MARKET_OPEN_H` to a past hour in `bot.py`.

---

## 4. Understanding the Logs

### `logs/bot.log`
Human-readable structured log of everything. Good for understanding what happened:
```
10:15:02 | INFO     | CYCLE #3 — 2024-01-15T15:15:00 UTC
10:15:03 | INFO     | SIGNAL | AAPL   | BUY  | reason=ema_crossover_up_rsi_confirmed | ...
10:15:03 | SUCCESS  | TRADE  | AAPL   | BUY  108 shares @ $185.42 | stop=$180.42 | tp=$195.42
10:15:04 | INFO     | SIGNAL | MSFT   | HOLD | reason=no_signal | ...
```

### `logs/signals.csv`
Every signal evaluation for every symbol every cycle. Use this to answer:
- "Why didn't the bot buy X at time Y?" → find the row, read the `reason` and `action_taken` columns
- "How often does each signal fire?" → count by `signal` column

### `logs/trades.csv`
Only actual order submissions. Columns include entry price, stop, take-profit, position size, risk amount, order ID. Use to calculate:
- Win rate (cross-reference with Alpaca's order history)
- Average R:R
- Position sizing accuracy

### `logs/portfolio.csv`
Equity curve snapshot at each cycle. Import into pandas/Excel and plot `equity` over time.

---

## 5. Deploy to Railway

### Step 1: Create a Railway project

1. Go to [railway.app](https://railway.app) and sign in
2. Click **New Project** → **Deploy from GitHub repo**
3. Connect your GitHub account and select the repo containing this code
   - If you haven't pushed to GitHub yet: `git init && git add . && git commit -m "initial" && git remote add origin <your-repo-url> && git push -u origin main`

### Step 2: Set environment variables

In your Railway project → **Variables** tab, add:

| Variable | Value |
|----------|-------|
| `ALPACA_API_KEY` | your paper trading API key |
| `ALPACA_SECRET_KEY` | your paper trading secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` |

Do NOT commit your `.env` file — `.gitignore` already excludes it.

### Step 3: Add a persistent volume (optional but recommended)

Without a volume, logs are lost on every redeploy. To persist logs:

1. In Railway project → **Add Service** → **Volume**
2. Mount path: `/data`
3. Add variable: `LOG_DIR=/data/logs`

This keeps `signals.csv`, `trades.csv`, and `portfolio.csv` across restarts.

### Step 4: Deploy

Railway auto-deploys on every push to main. The `railway.toml` tells it to:
- Build with the `Dockerfile`
- Run `python bot.py` as a worker (not a web server)
- Restart up to 10 times on failure

### Viewing logs on Railway

```bash
# Install Railway CLI
npm i -g @railway/cli
railway login
railway logs
```

Or view in the Railway web UI under your service → **Logs** tab.

---

## 6. Monitoring Your Bot

### Quick health check

The bot logs `CYCLE #N` at the start of every trading cycle. In Railway logs, you should see new cycle entries every 15 minutes during market hours. If you don't see them, check:

1. Environment variables are set correctly
2. The service is not in a crash loop (Railway shows restart count)
3. Alpaca API credentials are valid (the bot prints an error on startup if they fail)

### Check open positions

```bash
# Via Alpaca web UI
# Go to app.alpaca.markets → Paper Trading → Positions
```

### Download and analyze logs

If using a Railway volume, download via:
```bash
railway shell
cp /data/logs/*.csv /tmp/
# Then scp or download via Railway's file browser
```

---

## 7. Stopping the Bot

**Locally**: Ctrl+C — the bot handles SIGINT gracefully and exits cleanly.

**On Railway**: Go to your service → **Settings** → **Suspend Service**. This stops the process without deleting it.

To stop and close all positions manually, use Alpaca's web UI:
- Paper Trading → Positions → **Liquidate All**

---

## 8. Modifying the Strategy

All tuneable parameters are at the top of each file:

| File | Parameters |
|------|-----------|
| `strategy.py` | `FAST_EMA_PERIOD`, `SLOW_EMA_PERIOD`, `RSI_PERIOD`, `RSI_ENTRY_MIN`, `RSI_ENTRY_MAX`, `RSI_EXIT_OVERBOUGHT` |
| `risk_manager.py` | `RISK_PER_TRADE_PCT`, `ATR_STOP_MULTIPLIER`, `ATR_PROFIT_MULTIPLIER`, `MAX_POSITIONS`, `MAX_POSITION_PCT` |
| `bot.py` | `UNIVERSE`, `MARKET_OPEN_H/M`, `MARKET_CLOSE_H/M`, `BARS_TO_FETCH` |

See `STRATEGY.md` for the reasoning behind each default value before changing them.

---

## 9. Troubleshooting

**`KeyError: ALPACA_API_KEY`**
→ `.env` file is missing or not in the working directory. Run from the `autonomous-bot/` directory.

**`alpaca.common.exceptions.APIError: 403`**
→ Using live API keys with paper endpoint (or vice versa). Paper keys only work at `paper-api.alpaca.markets`.

**`No bars returned for AAPL`**
→ Usually happens before market open or after market close. The IEX feed has no pre-market data. Expected behavior.

**`nan_indicator` signals**
→ A gap in the bar data caused NaN to propagate through the EMA calculation. The bot handles this gracefully (returns HOLD). Will resolve itself as more bars accumulate.

**Bot runs but never places orders**
→ Check `signals.csv` for `action_taken` column. Common reasons: `max_positions_reached`, `already_holding_SYMBOL`, `skipped_no_data`, `skipped_size_too_small`.
