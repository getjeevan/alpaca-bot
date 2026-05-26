"""
Daily trading report — composes a human-readable summary from Alpaca.

Run standalone at market close (cron), or called from bot.py.
Designed to be readable by someone who doesn't want to look at log files.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from dotenv import load_dotenv

from notify import broadcast

load_dotenv()

log = logging.getLogger("report")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

ET = ZoneInfo("America/New_York")


def get_today_fills(trading: TradingClient) -> list:
    """Return all filled orders from today (US/Eastern) as simple dicts."""
    now_et = datetime.now(ET)
    today_start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_et.astimezone(timezone.utc)

    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=today_start_utc,
        limit=500,
    )
    try:
        orders = trading.get_orders(filter=req)
    except Exception as e:
        log.error(f"Failed to fetch orders: {e}")
        return []

    fills = []
    for o in orders:
        if o.filled_at is None or o.filled_avg_price is None or o.filled_qty is None:
            continue
        fills.append({
            "symbol": o.symbol,
            "side": str(o.side).lower(),
            "qty": float(o.filled_qty),
            "price": float(o.filled_avg_price),
            "time": o.filled_at,
            "value": float(o.filled_qty) * float(o.filled_avg_price),
        })
    return fills


def classify_fills(fills: list) -> dict:
    """Pair buys with sells per symbol to compute per-trade P&L where possible."""
    buys, sells = [], []
    for f in fills:
        (buys if "buy" in f["side"] else sells).append(f)

    # Naive FIFO pairing: each sell matches the earliest unmatched buy on same symbol
    round_trips = []
    buys_by_sym = {}
    for b in sorted(buys, key=lambda x: x["time"]):
        buys_by_sym.setdefault(b["symbol"], []).append(b)

    for s in sorted(sells, key=lambda x: x["time"]):
        queue = buys_by_sym.get(s["symbol"], [])
        if queue:
            b = queue.pop(0)
            pnl = s["value"] - b["value"]
            pnl_pct = (s["price"] - b["price"]) / b["price"] * 100
            round_trips.append({
                "symbol": s["symbol"],
                "entry": b["price"],
                "exit": s["price"],
                "qty": s["qty"],
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "entry_time": b["time"],
                "exit_time": s["time"],
            })

    return {
        "buys": buys,
        "sells": sells,
        "round_trips": round_trips,
        "open_buys": [b for q in buys_by_sym.values() for b in q],
    }


def render_report(account, positions, trades: dict) -> tuple[str, str, list]:
    """Return (subject, plain_text_body, slack_blocks)."""
    initial_capital = float(os.getenv("INITIAL_CAPITAL", "1000"))
    equity = float(account.equity)
    cash = float(account.cash)
    daytrades = int(account.daytrade_count)
    pdt_limit = int(os.getenv("PDT_LIMIT", "3"))

    pnl_total = equity - initial_capital
    pnl_pct = (pnl_total / initial_capital) * 100

    # Today's realized P&L from round trips
    realized_today = sum(t["pnl"] for t in trades["round_trips"])

    today_et = datetime.now(ET).strftime("%A, %b %d %Y")
    subject = f"Trading Bot · {today_et} · {'+' if realized_today >= 0 else ''}${realized_today:.2f}"

    # ─── Plain text body ───
    lines = [
        f"Trading Summary · {today_et}",
        "=" * 50,
        "",
        "ACCOUNT",
        f"  Equity:         ${equity:,.2f}",
        f"  Cash:           ${cash:,.2f}",
        f"  Total P&L:      {'+' if pnl_total >= 0 else ''}${pnl_total:,.2f}  ({pnl_pct:+.2f}% vs initial ${initial_capital:,.0f})",
        f"  Today realized: {'+' if realized_today >= 0 else ''}${realized_today:,.2f}",
        f"  PDT usage:      {daytrades}/{pdt_limit} day-trades (rolling 5 biz days)",
        "",
    ]

    if trades["round_trips"]:
        lines.append(f"CLOSED TODAY ({len(trades['round_trips'])} round trips)")
        for t in trades["round_trips"]:
            entry_t = t["entry_time"].astimezone(ET).strftime("%H:%M")
            exit_t = t["exit_time"].astimezone(ET).strftime("%H:%M")
            lines.append(
                f"  {t['symbol']:<5}  "
                f"${t['entry']:>6.2f} → ${t['exit']:>6.2f}  "
                f"{t['pnl_pct']:>+6.2f}%  "
                f"{'+' if t['pnl'] >= 0 else ''}${t['pnl']:>6.2f}  "
                f"({entry_t}→{exit_t})"
            )
        lines.append("")

    if positions:
        lines.append(f"OPEN POSITIONS ({len(positions)})")
        for p in positions:
            entry = float(p.avg_entry_price)
            current = float(p.current_price) if p.current_price else 0
            qty = float(p.qty)
            mv = float(p.market_value) if p.market_value else 0
            unreal_pct = (current - entry) / entry * 100 if entry else 0
            unreal_abs = (current - entry) * qty
            lines.append(
                f"  {p.symbol:<5}  "
                f"qty={qty:<7.3f}  "
                f"entry=${entry:>6.2f}  "
                f"now=${current:>6.2f}  "
                f"{unreal_pct:>+6.2f}%  "
                f"{'+' if unreal_abs >= 0 else ''}${unreal_abs:>6.2f}  "
                f"mv=${mv:.2f}"
            )
        lines.append("")

    if not trades["round_trips"] and not positions:
        lines.append("No activity today. Bot watching, no entries or exits triggered.")
        lines.append("")

    # Alerts
    alerts = []
    dd_pct = (equity - initial_capital) / initial_capital * 100
    if dd_pct <= float(os.getenv("DD_KILL", "-0.05")) * 100:
        alerts.append(f"⚠ KILL SWITCH TERRITORY · drawdown {dd_pct:.2f}%")
    elif dd_pct <= float(os.getenv("DD_CIRCUIT", "-0.03")) * 100:
        alerts.append(f"⚠ CIRCUIT BREAKER · drawdown {dd_pct:.2f}% · entries paused")
    if daytrades >= pdt_limit:
        alerts.append(f"⚠ PDT budget exhausted ({daytrades}/{pdt_limit}) · take-profits will hold overnight")

    if alerts:
        lines.append("ALERTS")
        for a in alerts:
            lines.append(f"  {a}")
        lines.append("")

    lines.append("-" * 50)
    lines.append(f"Generated {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')}")

    text_body = "\n".join(lines)

    # ─── Slack blocks ───
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Trading Report · {today_et}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Equity*\n${equity:,.2f}"},
                {"type": "mrkdwn", "text": f"*Total P&L*\n{'+' if pnl_total >= 0 else ''}${pnl_total:,.2f} ({pnl_pct:+.2f}%)"},
                {"type": "mrkdwn", "text": f"*Today Realized*\n{'+' if realized_today >= 0 else ''}${realized_today:,.2f}"},
                {"type": "mrkdwn", "text": f"*PDT*\n{daytrades}/{pdt_limit}"},
            ],
        },
    ]

    if trades["round_trips"]:
        lines_s = [
            f"`{t['symbol']:<5}` ${t['entry']:.2f} → ${t['exit']:.2f}  *{t['pnl_pct']:+.2f}%*  "
            f"({'+' if t['pnl'] >= 0 else ''}${t['pnl']:.2f})"
            for t in trades["round_trips"]
        ]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Closed Today*\n" + "\n".join(lines_s)},
        })

    if positions:
        lines_p = []
        for p in positions:
            entry = float(p.avg_entry_price)
            current = float(p.current_price) if p.current_price else 0
            unreal_pct = (current - entry) / entry * 100 if entry else 0
            lines_p.append(f"`{p.symbol:<5}` entry ${entry:.2f} · now ${current:.2f} · *{unreal_pct:+.2f}%*")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Open Positions*\n" + "\n".join(lines_p)},
        })

    if alerts:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Alerts*\n" + "\n".join(alerts)},
        })

    return subject, text_body, blocks


def main() -> None:
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not secret:
        log.error("Missing Alpaca credentials")
        sys.exit(1)

    trading = TradingClient(api_key, secret, paper=paper)

    account = trading.get_account()
    positions = trading.get_all_positions()
    fills = get_today_fills(trading)
    trades = classify_fills(fills)

    subject, text_body, blocks = render_report(account, positions, trades)

    print(text_body)  # always log to stdout
    broadcast(subject, text_body, slack_blocks=blocks)


if __name__ == "__main__":
    main()
