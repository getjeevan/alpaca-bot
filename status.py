"""
Midday status update — posts a snapshot to Slack.
Runs at 12:00 PM ET via cron: 0 17 * * 1-5
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from notify import send_slack

load_dotenv()

ET = ZoneInfo("America/New_York")


def main() -> None:
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret  = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper   = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    initial = float(os.getenv("INITIAL_CAPITAL", "100000"))
    pdt_lim = int(os.getenv("PDT_LIMIT", "3"))

    if not api_key or not secret:
        sys.exit(1)

    trading   = TradingClient(api_key, secret, paper=paper)
    account   = trading.get_account()
    positions = trading.get_all_positions()

    equity      = float(account.equity)
    cash        = float(account.cash)
    daytrades   = int(account.daytrade_count)
    last_equity = float(account.last_equity)
    today_pl    = equity - last_equity
    total_pl    = equity - initial
    dd_pct      = (total_pl / initial) * 100
    today_pct   = (today_pl / last_equity) * 100 if last_equity else 0

    now_str = datetime.now(ET).strftime("%I:%M %p ET")

    pl_emoji  = "📈" if today_pl >= 0 else "📉"
    dd_emoji  = "🟢" if dd_pct >= 0 else "🔴"

    lines = [
        f"*📊 Midday Update · {now_str}*",
        "",
        f"*Equity:* ${equity:,.2f}",
        f"*Cash:*   ${cash:,.2f}",
        f"{pl_emoji} *Today P&L:* {'+' if today_pl >= 0 else ''}${today_pl:,.2f} ({today_pct:+.2f}%)",
        f"{dd_emoji} *Total P&L:* {'+' if total_pl >= 0 else ''}${total_pl:,.2f} ({dd_pct:+.2f}%)",
        f"*PDT used:* {daytrades}/{pdt_lim}",
        "",
    ]

    if positions:
        lines.append(f"*Open positions ({len(positions)}):*")
        for p in positions:
            entry   = float(p.avg_entry_price)
            current = float(p.current_price) if p.current_price else entry
            pnl_pct = (current - entry) / entry * 100
            emoji   = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"  {emoji} `{p.symbol}` entry ${entry:.2f} → ${current:.2f} "
                f"(*{pnl_pct:+.2f}%*)"
            )
    else:
        lines.append("_No open positions_")

    msg = "\n".join(lines)
    send_slack(msg)
    # Echo to stdout so cron logs confirm delivery
    print(f"[status.py] Slack update sent at {now_str}")
    print(f"[status.py] Equity=${equity:,.2f}  Today={today_pl:+,.2f}  Positions={len(positions)}")


if __name__ == "__main__":
    main()
