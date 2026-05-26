# Alpaca Paper Trading Bot

Small-cap scalper · $1,000 budget · +5% take-profit · -2.5% stop-loss · PDT-compliant · with Slack/email notifications and daily P&L reports.

Paper trading only. Validate for 60+ sessions before considering live.

---

## Project layout

```
alpaca-bot/
├── bot.py            # main trading loop
├── report.py         # daily P&L summary (run at market close)
├── notify.py         # Slack webhook + SMTP email helpers
├── requirements.txt
├── .env.example      # config template
├── Procfile          # Railway: worker + report processes
├── .gitignore
└── README.md
```

---

## What it does

**`bot.py` — runs continuously during market hours:**

1. Pulls account state from Alpaca: equity, cash, `daytrade_count`.
2. Drawdown checks:
   - **Kill switch** at -5%: halts everything, sends Slack/email alert, requires manual reset.
   - **Circuit breaker** at -3%: pauses new entries, still manages existing positions.
3. For each of 10 watchlist symbols: fetches latest trade + today's open in one snapshot call.
4. Exit logic on held positions:
   - +5% and PDT budget available → sell (take-profit).
   - +5% but PDT exhausted → hold overnight (converts to swing, avoids PDT violation).
   - -2.5% → always sells (capital preservation > PDT budget preservation).
5. Entry logic on flat positions: buy $100 notional when price is in [-1.5%, +1.0%] from open, none of the blockers apply.
6. Fires Slack message on every BUY, SELL, STOP_LOSS, and kill switch event.

All state (positions, cash, PDT counter) lives in Alpaca. The bot is stateless — crash-safe and cron-friendly.

**`report.py` — run at market close (4:05pm ET):**

Produces a daily summary: equity, total P&L, today's realized P&L, round-trip trades paired entry→exit with P&L, open positions with unrealized P&L, PDT usage, and any active alerts. Sent to Slack, email, or both.

---

## Step 1 — Alpaca paper account (5 min)

1. Sign up at **https://alpaca.markets** — free, takes email + basics. No SSN required for paper.
2. Switch to the **Paper Trading** dashboard (top-left toggle).
3. Right sidebar → **API Keys** → **Generate New Key**.
4. Copy the **Key ID** (starts with `PK`) and **Secret Key** (shown once — save it).

Paper account starts with $100k simulated. The bot only uses $1k of it per the `INITIAL_CAPITAL` setting.

## Step 2 — Local setup (10 min)

Requires Python 3.11+.

```bash
git clone <your-repo> alpaca-bot
cd alpaca-bot
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and paste ALPACA_API_KEY and ALPACA_SECRET_KEY
```

**First run — dry mode (logs signals, no orders):**
```bash
DRY_RUN=true RUN_ONCE=true python bot.py
```

**Paper live run:**
```bash
python bot.py
```

Leave running during market hours (9:30am–4pm ET). Watch for BUY, HOLD, TAKE_PROFIT, STOP_LOSS events in logs.

**Generate today's report any time:**
```bash
python report.py
```

---

## Step 3 — Notifications (optional but recommended)

Both channels are fully optional. If env vars are blank, that channel just doesn't fire — no errors.

### 3a. Slack webhook (2 min)

The bot uses an **Incoming Webhook** which only requires the webhook URL.

1. Go to **https://api.slack.com/apps** → **Create New App** → "From scratch".
2. Name it (e.g. "Trading Bot"), pick your workspace.
3. In the app settings, click **Incoming Webhooks** → toggle on.
4. Click **Add New Webhook to Workspace** → pick the channel → Allow.
5. Copy the webhook URL (looks like `https://hooks.slack.com/services/T.../B.../...`).
6. Paste into `.env`:
   ```
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
   ```

**Test it:**
```bash
python -c "from notify import send_slack; print(send_slack('Bot connected ✓'))"
```

Should print `True` and post "Bot connected ✓" to the channel.

### 3b. Email via Gmail SMTP (5 min)

Gmail requires an **App Password** — your regular password won't work, and this is safer than storing your real password anywhere.

1. Make sure 2-Step Verification is on for your Google account.
2. Go to **https://myaccount.google.com/apppasswords**.
3. App name: "Trading Bot" → Generate.
4. Copy the 16-character password (4 groups of 4, spaces don't matter).
5. Paste into `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=yourname@gmail.com
   SMTP_PASSWORD=xxxxxxxxxxxxxxxx
   EMAIL_FROM=yourname@gmail.com
   EMAIL_TO=owner@example.com
   ```

**Test it:**
```bash
python -c "from notify import send_email; print(send_email('Bot test', 'Bot connected ✓'))"
```

Should print `True` and deliver an email.

Non-Gmail SMTP works too — just update `SMTP_HOST` and `SMTP_PORT` (e.g. Fastmail: `smtp.fastmail.com:465`, Outlook: `smtp-mail.outlook.com:587`).

---

## Step 4 — Deploy to Railway 24/7 ($5/mo)

Railway runs the bot as a long-running worker and the report as a scheduled task.

1. Push this folder to a GitHub repo (private is fine — the `.gitignore` keeps `.env` out).
2. **https://railway.app** → New Project → Deploy from GitHub repo.
3. Railway detects `requirements.txt` and `Procfile`. The `Procfile` declares two processes:
   - `worker: python bot.py` — runs continuously, restarts on failure.
   - `report: python report.py` — one-shot, scheduled via cron.
4. **Variables** tab → add every line from your `.env` (all of them: Alpaca keys, strategy params, notification creds). Never commit `.env`.
5. **Settings → Cron Schedule** for the `report` service: `5 20 * * 1-5` (4:05pm ET ≈ 20:05 UTC during EDT, or `5 21 * * 1-5` during EST). This fires the report once after market close each weekday.
6. Deploy. Check **Logs** to confirm the worker is running and the report fires at the scheduled time.

### Alternative — GitHub Actions (free)

If you don't want to pay for Railway, use GitHub Actions cron for both the bot (every 5 min) and the report (once daily). Create `.github/workflows/bot.yml`:

```yaml
name: Trading Bot
on:
  schedule:
    - cron: "*/5 13-20 * * 1-5"   # bot: every 5 min, 9:00am-4:00pm ET (13-20 UTC during EDT)
    - cron: "10 20 * * 1-5"       # report: 4:10pm ET daily
  workflow_dispatch:

jobs:
  trade:
    if: github.event.schedule != '10 20 * * 1-5'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - run: RUN_ONCE=true python bot.py
        env:
          ALPACA_API_KEY: ${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY: ${{ secrets.ALPACA_SECRET_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          # ... repeat for every var in .env

  report:
    if: github.event.schedule == '10 20 * * 1-5'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - run: python report.py
        env:
          ALPACA_API_KEY: ${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY: ${{ secrets.ALPACA_SECRET_KEY }}
          # ... repeat for every var
```

Add all env vars under **Settings → Secrets and variables → Actions**. Completely free. Downside: 5-minute minimum cron interval, which is fine for this strategy.

---

## Step 5 — After 60 trading sessions

Pull Alpaca trade history (dashboard → Activity → Export CSV) or aggregate `report.py` emails. Compute:

- **Win rate**: % of closed trades that were profitable.
- **Avg win %** vs **avg loss %**: with +5% TP and -2.5% SL, you need >~40% win rate to break even after slippage.
- **Expectancy per trade**: `(win_rate × avg_win) − (loss_rate × avg_loss)`. Must be positive.
- **Max drawdown**: biggest peak-to-trough equity decline.
- **PDT-blocked exits**: if high, strategy is capital-constrained by PDT.

If expectancy is positive and max drawdown <10%, consider a live account. If negative, adjust `ENTRY_BAND_LOW/HIGH` or rotate the watchlist and run another 60 sessions.

---

## Configuration reference

| Var | Default | What it does |
|---|---|---|
| `INITIAL_CAPITAL` | 1000 | Reference for drawdown |
| `ALLOCATION_PER_STOCK` | 100 | Dollar notional per position |
| `TAKE_PROFIT` | 0.05 | +5% exit |
| `STOP_LOSS` | -0.025 | -2.5% exit (always fires) |
| `DD_CIRCUIT` | -0.03 | Pause entries at -3% equity drawdown |
| `DD_KILL` | -0.05 | Halt at -5% (notifies + manual reset) |
| `PDT_LIMIT` | 3 | Max day-trades per rolling 5 biz days |
| `ENTRY_BAND_LOW` | -0.015 | Buy if price ≥ this % from open |
| `ENTRY_BAND_HIGH` | 0.01 | Buy if price ≤ this % from open |
| `WATCHLIST` | 10 tickers | Comma-separated |
| `INTERVAL_SECONDS` | 60 | Cycle frequency |
| `RUN_ONCE` | false | Run one cycle and exit (cron mode) |
| `DRY_RUN` | false | Log signals, place no orders |
| `SLACK_WEBHOOK_URL` | — | Optional Slack notifications |
| `SMTP_*` / `EMAIL_*` | — | Optional email notifications |

---

## Rotating the watchlist

Monthly screen for small-caps matching:
- Market cap $300M–$2B
- Avg daily volume >500k shares (fills with minimal slippage)
- Price $1–$15 (so $100 buys a meaningful share count)
- Sector variety

Use **Finviz** (https://finviz.com/screener.ashx) or **TradingView** to screen. Paste 10 symbols into `WATCHLIST` env var and redeploy.

---

## Troubleshooting

**"403 Forbidden"** — wrong key/environment. Confirm `ALPACA_PAPER=true` and keys come from the Paper dashboard.

**"Insufficient buying power"** — shouldn't happen on paper (starts at $100k). On live, match `INITIAL_CAPITAL` to funded amount.

**No trades executing** — check logs for `blocked: band`. Entry band is deliberately narrow. To widen: `ENTRY_BAND_LOW=-0.025`, `ENTRY_BAND_HIGH=0.02`.

**PDT counter mismatch** — `daytrade_count` is authoritative from Alpaca. Day trade = same symbol bought and sold (or vice versa) within the same trading day.

**Slack not firing** — test with `python -c "from notify import send_slack; print(send_slack('hi'))"`. Should print `True`. If `False`, check webhook URL.

**Email not firing** — test with `python -c "from notify import send_email; print(send_email('t', 'body'))"`. Gmail requires an App Password, not your real password.

---

## Risk disclosure

Paper trading has no financial risk. Live trading does. Past paper results don't guarantee live results — slippage, overnight gaps, and execution delays degrade performance. Do not go live without positive expectancy over 60+ paper sessions. Provided as-is for educational use.
