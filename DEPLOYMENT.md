# Alpaca Bot Deployment & Integration Guide

## 🚀 Quick Start (Local Testing)

### 1. Install new dependencies
```bash
cd ~/alpaca
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Test bot locally (single cycle)
```bash
# Dry-run mode (no orders, just signals)
DRY_RUN=true RUN_ONCE=true python alpaca-bot/bot.py

# This will:
# - Load secrets from .env (dev_mode=True)
# - Initialize SQLite database (trading.db)
# - Run one trading cycle
# - Log trades to database
# - Check persistent cooldown state
```

### 3. Check database
```bash
sqlite3 alpaca-bot/trading.db

# Inside sqlite3:
SELECT * FROM trades ORDER BY created_at DESC LIMIT 10;
SELECT * FROM cooldowns;
.quit
```

---

## 🔐 Production Deployment (Hostinger VPS)

### Setup Phase: One-Time (Automated)

Run the setup script on the VPS:
```bash
# On your local machine:
# 1. Rotate credentials on https://app.alpaca.markets
# 2. Get new API key and secret key

# SSH to Hostinger VPS:
ssh root@187.124.240.44

# Run setup script (this will prompt for secrets):
cd /root/alpaca-bot
bash setup-secrets.sh
```

**What it does:**
- ✅ Moves secrets from `.env` to systemd `/etc/systemd/system/alpaca-bot.service.d/credentials.conf`
- ✅ Replaces `.env` with placeholder values (safe to commit)
- ✅ Installs systemd service files (`alpaca-bot.service`, `alpaca-bot-watchdog.service`)
- ✅ Enables auto-start on server reboot
- ✅ Creates `/var/log/alpaca-bot/` directory (optional, for file logging)

### Post-Setup: Testing & Enablement

#### Test 1: Manual single cycle
```bash
# SSH to VPS
ssh root@187.124.240.44

# Run bot once (single cycle, see if it connects to Alpaca)
cd /root/alpaca-bot
systemctl start alpaca-bot.service

# Monitor logs in real-time
journalctl -u alpaca-bot.service -f

# Verify database was created
sqlite3 trading.db "SELECT COUNT(*) FROM trades;"
```

#### Test 2: Watchdog health check
```bash
# Start watchdog service
systemctl start alpaca-bot-watchdog.service

# Monitor watchdog logs
journalctl -u alpaca-bot-watchdog.service -f

# Watchdog checks bot health every 60s, auto-restarts if hung
```

#### Test 3: Cron integration (keep your existing cron, or update to systemctl)
```bash
# Option A: Keep existing cron (call systemctl instead of direct command)
crontab -e

# Replace old:
# 13 30 * * 1-5 /root/alpaca-bot/venv/bin/python /root/alpaca-bot/bot.py
# With:
13 30 * * 1-5 systemctl restart alpaca-bot.service
20 00 * * 1-5 systemctl stop alpaca-bot.service
20 05 * * 1-5 systemctl restart alpaca-bot.service && /root/alpaca-bot/venv/bin/python /root/alpaca-bot/report.py

# Option B: Let systemd handle everything, disable cron (more robust)
# Just remove cron entries and let systemctl's continuous loop manage it
```

---

## 📊 Monitoring & Observability

### Real-Time Logs (Systemd Journal)
```bash
# Current bot logs
journalctl -u alpaca-bot.service -f

# Last 100 lines
journalctl -u alpaca-bot.service -n 100

# Filter by time
journalctl -u alpaca-bot.service --since "2 hours ago"

# Watchdog logs
journalctl -u alpaca-bot-watchdog.service -f
```

### Database Queries (Manual)
```bash
# SSH to VPS, then:
sqlite3 /root/alpaca-bot/trading.db

# Today's trades
SELECT symbol, side, qty, price, reason, signal_score, pnl_pct FROM trades
WHERE date(timestamp_utc) = date('now')
ORDER BY timestamp_utc DESC;

# Trade statistics (last 7 days)
SELECT
    COUNT(*) as total_trades,
    SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END) as buys,
    SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) as sells,
    AVG(pnl_pct) as avg_pnl_pct,
    COUNT(DISTINCT symbol) as unique_symbols
FROM trades
WHERE datetime(timestamp_utc) >= datetime('now', '-7 days');

# Active cooldowns
SELECT symbol, cooldown_until_utc, reason FROM cooldowns;

# Exit
.quit
```

### Future: Metrics & Dashboards
Once satisfied with bot stability, add:
```bash
# Prometheus + Grafana for real-time dashboards
# - Equity over time
# - Drawdown %
# - Win/loss ratio
# - Trade latency
# - API health
```

---

## 🛡️ Risk Management

### Kill Switch Manual Reset
If bot hits kill switch (-5% drawdown), it stops all trading. To reset:
```bash
# 1. Verify the issue is resolved (manually check positions, chat with yourself)
# 2. SSH to VPS
ssh root@187.124.240.44

# 3. Restart bot
systemctl restart alpaca-bot.service

# 4. Monitor
journalctl -u alpaca-bot.service -f
```

### Cooldown Management
If you need to force-clear a cooldown (e.g., mistaken stop-loss):
```bash
ssh root@187.124.240.44
sqlite3 /root/alpaca-bot/trading.db

DELETE FROM cooldowns WHERE symbol = 'IONQ';
.quit
```

---

## 🔄 Continuous Monitoring Checklist

### Daily
- [ ] Check `journalctl -u alpaca-bot.service` for errors
- [ ] Verify Slack alerts are arriving
- [ ] Check trade journal for unexpected behavior

### Weekly
- [ ] Run `sqlite3 trading.db` and review trade stats
- [ ] Check drawdown trend
- [ ] Review PDT usage
- [ ] Rotate Alpaca API keys (if required by your broker)

### Monthly
- [ ] Full audit of trading decisions
- [ ] Backtest strategy against new market data
- [ ] Archive old database (if growing large)
- [ ] Review systemd service status

---

## 🐛 Troubleshooting

### Bot won't start
```bash
# Check systemd status
systemctl status alpaca-bot.service

# View full logs
journalctl -u alpaca-bot.service -n 50

# Common issues:
# - Missing ALPACA_API_KEY in /etc/systemd/system/alpaca-bot.service.d/credentials.conf
# - Python venv path wrong
# - Database locked (another instance running)
```

### Watchdog constantly restarting bot
```bash
# Watchdog checks every 60s, restarts after 3 consecutive failures
# Check bot logs for the actual error:
journalctl -u alpaca-bot.service -f
```

### Database locked
```bash
# If you see "database is locked" error:
# - Only one bot instance should be running
# - Check for orphaned processes:
ps aux | grep bot.py

# Kill if necessary:
kill -9 <PID>

# Then restart:
systemctl restart alpaca-bot.service
```

---

## 📝 File Structure

After setup, your VPS should have:

```
/root/alpaca-bot/
├── bot.py                    # Main trading engine
├── report.py                 # Daily P&L report
├── status.py                 # Account status snapshot
├── notify.py                 # Slack + email notifications
├── secrets.py                # Credentials loader (NEW)
├── db.py                     # SQLite trade journal (NEW)
├── watchdog.py               # Health monitor (NEW)
├── trading.db                # SQLite database (auto-created)
├── .env                      # Placeholders only (NEW)
├── .env.backup.20260524_*    # Backup of old .env
├── setup-secrets.sh          # One-time setup script
├── alpaca-bot.service        # systemd service file (NEW)
├── alpaca-bot-watchdog.service # Watchdog service file (NEW)
└── requirements.txt          # Dependencies (no changes)

/etc/systemd/system/
├── alpaca-bot.service.d/
│   └── credentials.conf      # Secrets (systemd EnvironmentFile)
└── alpaca-bot-watchdog.service

/var/log/alpaca-bot/
└── bot.log                   # Optional: persistent logs (NEW)
```

---

## 🔑 Security Reminders

1. **Never commit `.env`** — it's now in `.gitignore`
2. **Remove from git history**:
   ```bash
   git filter-branch --force --index-filter 'git rm --cached --ignore-unmatch alpaca-bot/.env' \
     --prune-empty --tag-name-filter cat -- --all
   git push origin --force --all
   ```
3. **Rotate keys every 90 days** via Alpaca dashboard
4. **Lock down server SSH** — already done (key-only auth, fail2ban)
5. **Systemd credentials file is mode 600** — only root can read

---

## 📚 Next Steps

1. ✅ Run `setup-secrets.sh` on Hostinger VPS
2. ✅ Test bot with `systemctl start alpaca-bot.service`
3. ✅ Enable watchdog: `systemctl start alpaca-bot-watchdog.service`
4. ✅ Verify cron schedule is updated (or disabled)
5. ✅ Monitor logs for 24-48 hours
6. ✅ Archive old `.env` backup (or delete after confirming)

---

## 📞 Support

If bot crashes:
1. Check `journalctl -u alpaca-bot.service`
2. Check `journalctl -u alpaca-bot-watchdog.service`
3. Verify `/etc/systemd/system/alpaca-bot.service.d/credentials.conf` exists
4. Restart: `systemctl restart alpaca-bot.service`

Watchdog will auto-restart if it detects hung process. You'll see Slack alerts.
