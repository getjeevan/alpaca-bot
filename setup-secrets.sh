#!/bin/bash
# ═════════════════════════════════════════════════════════════════════════════
#  Setup Alpaca Bot Secrets Management
#  ═════════════════════════════════════════════════════════════════════════════
#  Run this ONCE on the Hostinger VPS to:
#  1. Move credentials from .env to systemd environment
#  2. Lock down .env (remove/prevent commits)
#  3. Set proper service permissions
#
#  Usage: sudo bash setup-secrets.sh
#  ═════════════════════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'  # No Color

BOT_DIR="/root/alpaca-bot"
SERVICE_DIR="/etc/systemd/system"

echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}Alpaca Bot Secrets Setup${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}\n"

# ─────────────────────── Check if running as root ───────────────────────
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}ERROR: This script must be run as root${NC}"
   exit 1
fi

# ─────────────────────── Step 1: Prompt for secrets ───────────────────────
echo -e "${YELLOW}Step 1: Enter your credentials${NC}"
echo "Leave empty to skip a credential (optional ones can be disabled)"
echo ""

read -sp "ALPACA_API_KEY: " ALPACA_API_KEY
echo ""
read -sp "ALPACA_SECRET_KEY: " ALPACA_SECRET_KEY
echo ""
read -p "ALPACA_PAPER (true/false) [default: true]: " ALPACA_PAPER
ALPACA_PAPER=${ALPACA_PAPER:-true}
echo ""

read -p "SLACK_WEBHOOK_URL (optional): " SLACK_WEBHOOK_URL
echo ""
read -p "SMTP_HOST (optional, for email alerts): " SMTP_HOST
read -p "SMTP_USER (optional): " SMTP_USER
read -sp "SMTP_PASSWORD (optional): " SMTP_PASSWORD
echo ""
read -p "EMAIL_FROM (optional): " EMAIL_FROM
read -p "EMAIL_TO (optional): " EMAIL_TO
echo ""

# ─────────────────────── Validation ───────────────────────
if [[ -z "$ALPACA_API_KEY" ]] || [[ -z "$ALPACA_SECRET_KEY" ]]; then
   echo -e "${RED}ERROR: Alpaca API key and secret are required${NC}"
   exit 1
fi

# ─────────────────────── Step 2: Create systemd environment file ───────────────────────
echo -e "\n${YELLOW}Step 2: Creating systemd environment file${NC}"

ENV_FILE="/etc/systemd/system/alpaca-bot.service.d/credentials.conf"
mkdir -p "$(dirname "$ENV_FILE")"

cat > "$ENV_FILE" << EOF
[Service]
Environment="ALPACA_API_KEY=$ALPACA_API_KEY"
Environment="ALPACA_SECRET_KEY=$ALPACA_SECRET_KEY"
Environment="ALPACA_PAPER=$ALPACA_PAPER"
EOF

if [[ -n "$SLACK_WEBHOOK_URL" ]]; then
    echo "Environment=\"SLACK_WEBHOOK_URL=$SLACK_WEBHOOK_URL\"" >> "$ENV_FILE"
fi

if [[ -n "$SMTP_HOST" ]]; then
    echo "Environment=\"SMTP_HOST=$SMTP_HOST\"" >> "$ENV_FILE"
    echo "Environment=\"SMTP_USER=$SMTP_USER\"" >> "$ENV_FILE"
    echo "Environment=\"SMTP_PASSWORD=$SMTP_PASSWORD\"" >> "$ENV_FILE"
    echo "Environment=\"EMAIL_FROM=$EMAIL_FROM\"" >> "$ENV_FILE"
    echo "Environment=\"EMAIL_TO=$EMAIL_TO\"" >> "$ENV_FILE"
fi

chmod 600 "$ENV_FILE"
echo -e "${GREEN}✓ Created $ENV_FILE (mode 600)${NC}"

# ─────────────────────── Step 3: Secure .env file ───────────────────────
echo -e "\n${YELLOW}Step 3: Securing .env file${NC}"

if [[ -f "$BOT_DIR/.env" ]]; then
    # Backup old .env
    cp "$BOT_DIR/.env" "$BOT_DIR/.env.backup.$(date +%Y%m%d_%H%M%S)"
    echo -e "${GREEN}✓ Backed up .env to .env.backup.${NC}"

    # Replace credentials with placeholders
    cat > "$BOT_DIR/.env" << 'EOF'
# ⚠️ SECRETS MOVED TO SYSTEMD
# Do NOT commit this file. Use /etc/systemd/system/alpaca-bot.service.d/credentials.conf instead.
# See setup-secrets.sh for details.

ALPACA_API_KEY=[MOVE_TO_SYSTEMD]
ALPACA_SECRET_KEY=[MOVE_TO_SYSTEMD]
ALPACA_PAPER=true

# ─── Strategy parameters (safe to commit) ───
INITIAL_CAPITAL=2000
ALLOCATION_PER_STOCK=200
TAKE_PROFIT=0.10
STOP_LOSS=-0.05
DD_CIRCUIT=-0.06
DD_KILL=-0.10
PDT_LIMIT=3
MAX_POSITIONS=5

# ─── Universe ───
WATCHLIST=IONQ,RGTI,BBAI,SOUN,OPEN,CHPT,PLUG,LAZR,RKLB,PRSO

# ─── Runtime ───
INTERVAL_SECONDS=60
RUN_ONCE=false
DRY_RUN=false
EOF
    chmod 644 "$BOT_DIR/.env"
    echo -e "${GREEN}✓ Replaced .env credentials with placeholders${NC}"
fi

# ─────────────────────── Step 4: Install systemd services ───────────────────────
echo -e "\n${YELLOW}Step 4: Installing systemd services${NC}"

cp "$BOT_DIR/alpaca-bot.service" "$SERVICE_DIR/"
cp "$BOT_DIR/alpaca-bot-watchdog.service" "$SERVICE_DIR/"

systemctl daemon-reload
echo -e "${GREEN}✓ Installed alpaca-bot.service and alpaca-bot-watchdog.service${NC}"

# ─────────────────────── Step 5: Enable and start services ───────────────────────
echo -e "\n${YELLOW}Step 5: Enable services (startup on reboot)${NC}"

systemctl enable alpaca-bot.service
systemctl enable alpaca-bot-watchdog.service
echo -e "${GREEN}✓ Services enabled for auto-start${NC}"

# ─────────────────────── Summary ───────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Setup Complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Next steps:"
echo ""
echo "1. Test the bot (single cycle):"
echo "   systemctl start alpaca-bot.service"
echo "   journalctl -u alpaca-bot.service -f"
echo ""
echo "2. Once verified, enable watchdog:"
echo "   systemctl start alpaca-bot-watchdog.service"
echo "   journalctl -u alpaca-bot-watchdog.service -f"
echo ""
echo "3. Update cron schedule to use systemctl instead of direct command:"
echo "   crontab -e"
echo "   # Comment out or remove old alpaca-bot entries"
echo "   # Cron can still call: systemctl restart alpaca-bot.service"
echo ""
echo "4. Remove .env from git history (on your local machine):"
echo "   cd ~/alpaca"
echo "   git filter-branch --force --index-filter 'git rm --cached --ignore-unmatch alpaca-bot/.env' --prune-empty --tag-name-filter cat -- --all"
echo "   git push origin --force --all"
echo ""
echo "Credentials are now in: $ENV_FILE"
echo "Never commit .env to git."
echo ""
