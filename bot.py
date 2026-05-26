"""
═══════════════════════════════════════════════════════════════════════
  ALPACA PAPER TRADING BOT · SMALL-CAP SCALPER v2
  Composite signal: VWAP + RSI(14) + Relative Volume + Bollinger Band
  $100k budget · PDT-compliant · circuit breaker & kill switch

  Run modes:
    python bot.py                    # long-running loop (default)
    RUN_ONCE=true python bot.py      # single cycle (for cron/Lambda)
    DRY_RUN=true python bot.py       # log signals but place no orders
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest

from notify import broadcast, send_slack
from secrets import Secrets
import db

ET = ZoneInfo("America/New_York")

# ─────────────────────────── Logging ───────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bot")


# ─────────────────────────── Configuration ───────────────────────────

@dataclass(frozen=True)
class Config:
    api_key: str
    secret_key: str
    paper: bool

    initial_capital: float
    allocation_per_stock: float
    take_profit: float          # e.g. 0.05 = +5%
    stop_loss: float            # e.g. -0.025 = -2.5%
    dd_circuit: float           # e.g. -0.03 = pause entries
    dd_kill: float              # e.g. -0.05 = halt all
    pdt_limit: int
    max_positions: int          # max concurrent open positions

    # Signal thresholds
    min_signal_score: int       # min score to enter (out of 4)
    vwap_dev_max: float         # price must be below VWAP by at least this (e.g. -0.005 = -0.5%)
    rsi_max: float              # RSI(14) must be below this (e.g. 45)
    min_rel_volume: float       # relative volume vs 20-bar avg (e.g. 1.5)

    watchlist: tuple[str, ...]
    interval_seconds: int
    run_once: bool
    dry_run: bool

    # Intraday session controls (Strategy B)
    entry_window_end: Optional[str]   # "HH:MM" ET — stop new entries after this; None = no limit
    force_close_time: Optional[str]   # "HH:MM" ET — force-close all positions at this time; None = disabled

    # Strategy B: Fibonacci-enhanced signals
    use_fib_signals: bool             # enable weekly 0.618 entry + 0.382/0.786 exits


def load_config(secrets: Secrets) -> Config:
    """Load trading config from environment variables (via Secrets)."""
    return Config(
        api_key=secrets.alpaca_api_key,
        secret_key=secrets.alpaca_secret_key,
        paper=secrets.alpaca_paper,
        initial_capital=float(os.getenv("INITIAL_CAPITAL", "100000")),
        allocation_per_stock=float(os.getenv("ALLOCATION_PER_STOCK", "1000")),
        take_profit=float(os.getenv("TAKE_PROFIT", "0.05")),
        stop_loss=float(os.getenv("STOP_LOSS", "-0.025")),
        dd_circuit=float(os.getenv("DD_CIRCUIT", "-0.03")),
        dd_kill=float(os.getenv("DD_KILL", "-0.05")),
        pdt_limit=int(os.getenv("PDT_LIMIT", "3")),
        max_positions=int(os.getenv("MAX_POSITIONS", "5")),
        min_signal_score=int(os.getenv("MIN_SIGNAL_SCORE", "2")),
        vwap_dev_max=float(os.getenv("VWAP_DEV_MAX", "-0.005")),
        rsi_max=float(os.getenv("RSI_MAX", "45.0")),
        min_rel_volume=float(os.getenv("MIN_REL_VOLUME", "1.5")),
        watchlist=tuple(
            s.strip().upper()
            for s in os.getenv(
                "WATCHLIST",
                "IONQ,RGTI,BBAI,SOUN,OPEN,CHPT,PLUG,LAZR,RKLB,PRSO"
            ).split(",")
            if s.strip()
        ),
        interval_seconds=int(os.getenv("INTERVAL_SECONDS", "60")),
        run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        entry_window_end=os.getenv("ENTRY_WINDOW_END", "").strip() or None,
        force_close_time=os.getenv("FORCE_CLOSE_TIME", "").strip() or None,
        use_fib_signals=os.getenv("USE_FIB_SIGNALS", "false").lower() == "true",
    )


# ─────────────────────────── Technical Indicators ───────────────────────────

def compute_rsi(prices: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder's RSI — returns None if insufficient data."""
    if len(prices) < period + 1:
        return None
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    val = rsi.dropna()
    return float(val.iloc[-1]) if not val.empty else None


def compute_bb_lower(prices: pd.Series, period: int = 20, std_mult: float = 2.0) -> Optional[float]:
    """Lower Bollinger Band — returns None if insufficient data."""
    if len(prices) < period:
        return None
    sma = prices.rolling(period).mean()
    std = prices.rolling(period).std()
    lower = sma - std_mult * std
    val = lower.dropna()
    return float(val.iloc[-1]) if not val.empty else None


def compute_session_vwap(df: pd.DataFrame) -> Optional[float]:
    """Session VWAP from minute bars: sum(typical_price * volume) / sum(volume)."""
    if df.empty or df["volume"].sum() == 0:
        return None
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return float((typical * df["volume"]).sum() / df["volume"].sum())


# ─────────────────────────── Market Data ───────────────────────────

def _retry(fn, retries: int = 3, delay: float = 2.0):
    """Call fn() up to `retries` times, sleeping `delay` seconds between attempts."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                log.warning(f"API call failed (attempt {attempt+1}/{retries}): {e} — retrying in {delay}s")
                time.sleep(delay)
            else:
                raise


def get_snapshots(data: StockHistoricalDataClient, symbols: tuple[str, ...]) -> dict:
    """Batch-fetch latest trade + daily bar for all symbols (with retry)."""
    req = StockSnapshotRequest(symbol_or_symbols=list(symbols))
    return _retry(lambda: data.get_stock_snapshot(req))


def get_session_bars(data: StockHistoricalDataClient, symbols: tuple[str, ...]) -> dict:
    """
    Fetch 1-min bars from today's market open (9:30 ET) to now.
    Tries IEX feed first; falls back to SIP if IEX returns empty.
    Returns dict of symbol -> DataFrame, or {} on failure.
    """
    now_et = datetime.now(ET)
    market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < market_open_et:
        return {}

    start_utc = market_open_et.astimezone(timezone.utc)
    # IEX free tier has a 15-minute delay — request bars up to 16 min ago
    # so the fetch window always falls within the available data range.
    # Guard: if we're in the first 16 min after open, no bars available yet.
    end_utc = datetime.now(timezone.utc) - timedelta(minutes=16)
    if end_utc <= start_utc:
        return {}  # too early in the session, no IEX bars available yet

    def _fetch(feed: str) -> dict:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=list(symbols),
                timeframe=TimeFrame.Minute,
                start=start_utc,
                end=end_utc,
                feed=feed,
            )
            bar_set = data.get_stock_bars(req)
        except Exception as e:
            log.warning(f"Minute bar fetch failed ({feed}): {e}")
            return {}

        try:
            full_df = bar_set.df
        except Exception as e:
            log.warning(f"Could not read bar DataFrame ({feed}): {e}")
            return {}

        if full_df.empty:
            return {}

        result = {}
        for symbol in symbols:
            try:
                df = full_df.xs(symbol, level="symbol").copy()
                df.index = pd.to_datetime(df.index, utc=True)
                if not df.empty:
                    result[symbol] = df
            except KeyError:
                pass   # symbol had no bars this session
        return result

    bars = _fetch("iex")
    if not bars:
        log.warning("IEX feed returned no bars — falling back to SIP feed")
        bars = _fetch("sip")
    if not bars:
        log.warning("SIP feed also returned no bars — falling back to yfinance")
        bars = _fetch_yfinance(symbols)
    if not bars:
        log.warning("yfinance also returned no bars — all signals will be score=0")
    return bars


def _fetch_yfinance(symbols: tuple[str, ...]) -> dict:
    """
    Fallback bar fetch via yfinance (free, no subscription needed).
    Downloads today's 1-min bars for all symbols in one batch call.
    Returns dict of symbol -> DataFrame with columns: open, high, low, close, volume.
    """
    try:
        tickers = " ".join(symbols)
        df_raw = yf.download(
            tickers=tickers,
            period="1d",
            interval="1m",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        log.warning(f"yfinance download failed: {e}")
        return {}

    if df_raw is None or df_raw.empty:
        return {}

    result = {}
    for symbol in symbols:
        try:
            if len(symbols) == 1:
                df = df_raw.copy()
            else:
                df = df_raw[symbol].copy()
            df = df.dropna(how="all")
            if df.empty:
                continue
            df.columns = [c.lower() for c in df.columns]
            df.index = pd.to_datetime(df.index, utc=True)
            result[symbol] = df
        except (KeyError, AttributeError):
            pass

    if result:
        log.info(f"yfinance: got bars for {len(result)}/{len(symbols)} symbols")
    return result


def extract_prices(snapshot) -> tuple[Optional[float], Optional[float]]:
    price = float(snapshot.latest_trade.price) if snapshot.latest_trade else None
    today_open = float(snapshot.daily_bar.open) if snapshot.daily_bar else None
    return price, today_open


# ─────────────────────────── Weekly Fibonacci Levels ───────────────────────────

# Module-level cache — reloaded once per trading day
_fib_cache: dict[str, dict] = {}
_fib_cache_date: Optional[str] = None


def load_fib_levels(symbols: tuple[str, ...]) -> dict[str, dict]:
    """
    Fetch 2yr weekly bars via yfinance and compute Fibonacci retracement levels.
    Result is cached for the entire trading day and reloaded each morning.

    Levels stored per symbol:
        high    — swing high (2yr lookback)
        low     — swing low before that high
        fib236  — 23.6% retracement
        fib382  — 38.2% retracement (first resistance after bounce from 0.618)
        fib500  — 50.0% retracement
        fib618  — 61.8% retracement (primary entry zone)
        fib786  — 78.6% retracement (last support before new low)
    """
    global _fib_cache, _fib_cache_date

    today = datetime.now(ET).strftime("%Y-%m-%d")
    if _fib_cache_date == today and _fib_cache:
        return _fib_cache

    log.info(f"Loading weekly Fibonacci levels for {len(symbols)} symbols...")
    try:
        raw = yf.download(
            list(symbols), period="2y", interval="1wk",
            auto_adjust=True, progress=False, threads=True,
        )
        closes = raw["Close"]
    except Exception as e:
        log.warning(f"Fibonacci weekly fetch failed: {e} — using stale cache")
        return _fib_cache

    result: dict[str, dict] = {}
    for sym in symbols:
        try:
            s = closes[sym].dropna() if len(symbols) > 1 else closes.dropna()
            if len(s) < 20:
                continue
            high_idx  = s.idxmax()
            high_val  = float(s[high_idx])
            before    = s[:high_idx]
            if len(before) < 4:
                continue
            low_val   = float(before.min())
            rng       = high_val - low_val
            result[sym] = {
                "high":   high_val,
                "low":    low_val,
                "fib236": high_val - 0.236 * rng,
                "fib382": high_val - 0.382 * rng,
                "fib500": high_val - 0.500 * rng,
                "fib618": high_val - 0.618 * rng,
                "fib786": high_val - 0.786 * rng,
            }
        except Exception:
            continue

    _fib_cache      = result
    _fib_cache_date = today
    hits = sum(1 for s in symbols if s in result)
    log.info(f"Fibonacci levels ready: {hits}/{len(symbols)} symbols")
    return result


# ─────────────────────────── Signal Scoring ───────────────────────────

def score_entry(
    symbol: str,
    price: float,
    df: Optional[pd.DataFrame],
    cfg: Config,
    fib_levels: dict,
) -> tuple[int, str]:
    """
    Composite entry signal score (0-5). Higher = more conviction.

      +1  price is below session VWAP (mean-reversion opportunity)
      +1  RSI(14) < cfg.rsi_max (not overbought / in pullback)
      +1  relative volume > cfg.min_rel_volume (real market interest)
      +1  price at or below lower Bollinger Band (stretched low)
      +1  price within ±3% of weekly 0.618 Fibonacci retracement (key support)

    Returns (score, detail_string).
    """
    if df is None or len(df) < 5:
        # Fibonacci check can still run even without intraday bars
        score = 0
        details: list[str] = []
        fib = fib_levels.get(symbol, {})
        fib618 = fib.get("fib618")
        if fib618 and fib618 > 0:
            dev = (price - fib618) / fib618
            if abs(dev) <= 0.03:
                score += 1
                details.append(f"fib618({dev*100:+.1f}%)")
        return score, "no_bars" if not details else ",".join(details)

    score = 0
    details: list[str] = []

    closes = df["close"]

    # ── 1. VWAP deviation ──
    vwap = compute_session_vwap(df)
    if vwap and vwap > 0:
        dev = (price - vwap) / vwap
        if dev <= cfg.vwap_dev_max:
            score += 1
            details.append(f"vwap{dev*100:+.1f}%")

    # ── 2. RSI(14) ──
    rsi = compute_rsi(closes, 14)
    if rsi is not None and rsi < cfg.rsi_max:
        score += 1
        details.append(f"rsi={rsi:.0f}")

    # ── 3. Relative volume ──
    if "volume" in df.columns and len(df) >= 10:
        window  = min(20, len(df))
        avg_vol = df["volume"].rolling(window).mean().iloc[-1]
        curr_vol = df["volume"].iloc[-1]
        rel_vol = (curr_vol / avg_vol) if avg_vol > 0 else 0
        if rel_vol >= cfg.min_rel_volume:
            score += 1
            details.append(f"vol={rel_vol:.1f}x")

    # ── 4. Bollinger Band lower touch ──
    bb_lower = compute_bb_lower(closes, 20, 2.0)
    if bb_lower is not None and bb_lower > 0 and price <= bb_lower * 1.005:
        score += 1
        details.append("bb_low")

    # ── 5. Weekly 0.618 Fibonacci retracement ──
    fib = fib_levels.get(symbol, {})
    fib618 = fib.get("fib618")
    if fib618 and fib618 > 0:
        dev = (price - fib618) / fib618
        if abs(dev) <= 0.03:          # within ±3% of the 0.618 level
            score += 1
            details.append(f"fib618({dev*100:+.1f}%)")

    detail_str = ",".join(details) if details else "no_signal"
    return score, detail_str


# ─────────────────────────── Order Execution ───────────────────────────

def submit_buy(
    cfg: Config,
    trading: TradingClient,
    symbol: str,
    price: float,
    score: int,
    detail: str,
    bars_source: Optional[str] = None,
) -> None:
    max_score = 5 if cfg.use_fib_signals else 4
    msg = (
        f"{symbol:<5} | {'DRY BUY' if cfg.dry_run else 'BUY':<11} | "
        f"${cfg.allocation_per_stock:.0f} @ ~${price:.2f} · score={score}/{max_score} [{detail}]"
    )
    if cfg.dry_run:
        log.info(msg)
        # Log to DB even in dry-run for audit trail
        db.log_trade(
            symbol=symbol,
            side="BUY",
            qty=max(1, int(cfg.allocation_per_stock / price)),
            price=price,
            reason="ENTRY_SIGNAL_DRY",
            signal_score=score,
            bars_source=bars_source or "unknown",
        )
        return
    try:
        # Limit order: whole shares at price + 0.2% to ensure fill without chasing
        qty = max(1, int(cfg.allocation_per_stock / price))
        limit_price = round(price * 1.002, 2)
        resp = trading.submit_order(LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            limit_price=limit_price,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        log.info(f"{msg} · qty={qty} limit=${limit_price:.2f} · id={resp.id}")
        # Log trade to database
        db.log_trade(
            symbol=symbol,
            side="BUY",
            qty=qty,
            price=limit_price,  # limit price, not market price
            reason="ENTRY_SIGNAL",
            signal_score=score,
            bars_source=bars_source or "unknown",
            order_id=resp.id,
        )
        send_slack(
            f"🟢 BUY `{symbol}` · {qty} shares @ limit ${limit_price:.2f} "
            f"· signal {score}/{max_score} [{detail}]"
        )
    except Exception as e:
        log.error(f"{symbol:<5} | BUY FAILED | {e}")


def submit_close(
    cfg: Config,
    trading: TradingClient,
    symbol: str,
    qty: float,
    reason: str,
    pnl: float,
    price: Optional[float] = None,
) -> None:
    if cfg.dry_run:
        log.info(f"{symbol:<5} | DRY {reason:<11} | qty={qty} pnl={pnl*100:+.2f}%")
        # Log to DB even in dry-run
        db.log_trade(
            symbol=symbol,
            side="SELL",
            qty=qty,
            price=price or 0.0,
            reason=f"{reason}_DRY",
            pnl_pct=pnl,
        )
        return
    try:
        trading.close_position(symbol)
        log.info(f"{symbol:<5} | {reason:<11} | qty={qty} pnl={pnl*100:+.2f}% · closed")
        # Log trade to database
        db.log_trade(
            symbol=symbol,
            side="SELL",
            qty=qty,
            price=price or 0.0,
            reason=reason,
            pnl_pct=pnl,
        )
        emoji = "✅" if pnl >= 0 else "🔴"
        send_slack(f"{emoji} {reason} `{symbol}` · pnl *{pnl*100:+.2f}%*")
    except Exception as e:
        log.error(f"{symbol:<5} | CLOSE FAILED | {reason} | {e}")


# ─────────────────────────── Main Cycle ───────────────────────────

# SL cooldown: symbol -> datetime when it can be re-entered (persisted in DB, survives restarts)
SL_COOLDOWN_MINUTES = 30


def run_cycle(cfg: Config, trading: TradingClient, data: StockHistoricalDataClient) -> None:
    """One full evaluation pass — exits first, then entries."""

    # ── Market hours ──
    clock = trading.get_clock()
    if not clock.is_open:
        log.info(f"Market closed. Next open: {clock.next_open}")
        return

    # ── Account ──
    account = trading.get_account()
    equity  = float(account.equity)
    cash    = float(account.cash)
    daytrades = int(account.daytrade_count)
    dd_pct  = (equity - cfg.initial_capital) / cfg.initial_capital

    log.info(
        f"Account | equity=${equity:,.2f} cash=${cash:,.2f} "
        f"DT={daytrades}/{cfg.pdt_limit} DD={dd_pct*100:+.2f}%"
    )

    # ── Kill switch ──
    if dd_pct <= cfg.dd_kill:
        msg = (
            f"KILL SWITCH · drawdown {dd_pct*100:.2f}% ≤ {cfg.dd_kill*100:.0f}% · "
            "all trading halted. Manual reset required."
        )
        log.critical(msg)
        broadcast(
            subject=f"⚠ Bot · KILL SWITCH · {dd_pct*100:.2f}%",
            text_body=f"{msg}\n\nEquity: ${equity:,.2f}",
        )
        return

    # ── Circuit breaker ──
    circuit_open = dd_pct <= cfg.dd_circuit
    if circuit_open:
        log.warning(
            f"CIRCUIT BREAKER · drawdown {dd_pct*100:.2f}% · "
            "new entries paused, existing positions managed."
        )

    # ── Current ET time (used for intraday session controls) ──
    now_et = datetime.now(ET)

    # ── Fetch data ──
    positions = {p.symbol: p for p in trading.get_all_positions()}
    log.info(f"Positions | {len(positions)}/{cfg.max_positions} open · {list(positions.keys()) or 'none'}")

    # ── Force-close all positions at configured time (Strategy B intraday mode) ──
    if cfg.force_close_time and positions:
        fc_h, fc_m = map(int, cfg.force_close_time.split(":"))
        force_close_dt = now_et.replace(hour=fc_h, minute=fc_m, second=0, microsecond=0)
        if now_et >= force_close_dt:
            log.info(f"FORCE CLOSE · {cfg.force_close_time} ET reached · closing {len(positions)} position(s)")
            for sym, pos in positions.items():
                submit_close(cfg, trading, sym, float(pos.qty), "FORCE_CLOSE",
                             (float(pos.current_price or pos.avg_entry_price) - float(pos.avg_entry_price))
                             / float(pos.avg_entry_price))
            return  # Skip entries after forced close

    # Fetch orders from last 7 calendar days (covers 5 trading days) for:
    #   - opened_today: day-trade tracking
    #   - pending_buys: duplicate-entry guard
    #   - entry_times: stale position detection
    try:
        recent_orders = trading.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500,
            after=datetime.now(timezone.utc) - timedelta(days=7),
        ))
        today_utc = datetime.now(timezone.utc).date()
        opened_today: set[str] = {
            o.symbol for o in recent_orders
            if str(o.side) == "OrderSide.BUY"
            and str(o.status) == "OrderStatus.FILLED"
            and o.filled_at and o.filled_at.date() == today_utc
        }
        # Symbols with an open (unfilled) buy order — block re-entry to avoid duplicates
        pending_buys: set[str] = {
            o.symbol for o in recent_orders
            if str(o.side) == "OrderSide.BUY"
            and str(o.status) in ("OrderStatus.NEW", "OrderStatus.PARTIALLY_FILLED", "OrderStatus.ACCEPTED")
        }
        # Most recent buy fill time per symbol — used to detect stale positions.
        # Must use MOST RECENT (not oldest) so a freshly re-entered position
        # doesn't inherit a stale timestamp from a previous closed position.
        entry_times: dict[str, datetime] = {}
        for o in recent_orders:
            if str(o.side) == "OrderSide.BUY" and str(o.status) == "OrderStatus.FILLED" and o.filled_at:
                if o.symbol not in entry_times or o.filled_at > entry_times[o.symbol]:
                    entry_times[o.symbol] = o.filled_at
    except Exception as e:
        log.warning(f"Could not fetch recent orders: {e} — assuming all positions are overnight")
        opened_today = set()
        pending_buys  = set()
        entry_times   = {}

    try:
        snapshots = get_snapshots(data, cfg.watchlist)
    except Exception as e:
        log.error(f"Snapshot fetch failed: {e}")
        return

    # Minute bars for signals — non-blocking, falls back to score=0 if unavailable
    bars_by_symbol = get_session_bars(data, cfg.watchlist)

    # Weekly Fibonacci levels — only loaded for Strategy B (USE_FIB_SIGNALS=true)
    fib_levels = load_fib_levels(cfg.watchlist) if cfg.use_fib_signals else {}

    # ── Per-symbol evaluation ──
    for symbol in cfg.watchlist:
        snap = snapshots.get(symbol)
        if not snap:
            log.warning(f"{symbol:<5} | no snapshot, skipping")
            continue

        price, today_open = extract_prices(snap)
        if price is None or today_open is None:
            log.warning(f"{symbol:<5} | incomplete price data, skipping")
            continue

        day_chg = (price - today_open) / today_open
        position = positions.get(symbol)

        # ── EXIT ──
        if position:
            entry = float(position.avg_entry_price)
            pnl   = (price - entry) / entry
            qty   = float(position.qty)

            # Skip if a close order is already pending (qty_available == 0)
            qty_available = float(getattr(position, "qty_available", qty) or qty)
            if qty_available <= 0:
                log.debug(f"{symbol:<5} | PENDING | close order already submitted, skipping")
                continue

            is_day_trade = symbol in opened_today

            # ── Stale position exit ──
            # If held 3+ days and P&L is stuck within ±1%, cut it and redeploy capital.
            # Skip if opened today OR if there is a pending unfilled buy order for this
            # symbol — a limit order may not fill until the next cycle, so entry_times
            # would still show the old fill date and trigger a false STALE on a brand-new entry.
            entry_time = entry_times.get(symbol)
            if entry_time and symbol not in opened_today and symbol not in pending_buys:
                days_held = (datetime.now(timezone.utc) - entry_time).days
                if days_held >= 3 and abs(pnl) <= 0.01:
                    log.info(
                        f"{symbol:<5} | STALE   | held {days_held}d pnl={pnl*100:+.2f}% "
                        f"— closing dead position, redeploying capital"
                    )
                    submit_close(cfg, trading, symbol, qty, "STALE", pnl)
                    if is_day_trade:
                        daytrades += 1
                    continue

            # ── Fibonacci-based smart exit ──
            # If price reaches the 0.382 retracement (natural resistance above the
            # 0.618 entry zone) and we're in profit, sell into that resistance.
            fib = fib_levels.get(symbol, {})
            fib382 = fib.get("fib382")
            fib786 = fib.get("fib786")

            fib_tp_hit = (
                fib382 is not None
                and price >= fib382 * 0.99   # at or within 1% below 0.382 level
                and pnl >= 0.01              # at least 1% in profit
            )
            fib_sl_hit = (
                fib786 is not None
                and price <= fib786 * 1.005  # broke below 0.786 — structural breakdown
                and pnl < 0                  # only cut losses, not winners
            )

            if fib_tp_hit:
                if is_day_trade and daytrades >= cfg.pdt_limit:
                    log.info(
                        f"{symbol:<5} | HOLD    | pnl={pnl*100:+.2f}% · "
                        f"fib 0.382 resistance ${fib382:.2f} hit but PDT exhausted"
                    )
                else:
                    log.info(f"{symbol:<5} | fib 0.382 resistance at ${fib382:.2f} — taking profit")
                    submit_close(cfg, trading, symbol, qty, "FIB_TP", pnl)
                    if is_day_trade:
                        daytrades += 1
            elif fib_sl_hit:
                log.info(f"{symbol:<5} | fib 0.786 breakdown at ${fib786:.2f} — cutting loss")
                submit_close(cfg, trading, symbol, qty, "FIB_SL", pnl, price=price)
                if is_day_trade:
                    daytrades += 1
                # Cooldown: block re-entry on this symbol for 30 min (persisted to DB)
                cooldown_until = (now_et + timedelta(minutes=SL_COOLDOWN_MINUTES)).isoformat()
                db.set_cooldown(symbol, cooldown_until, reason="FIB_SL")
                log.info(f"{symbol:<5} | COOLDOWN | re-entry blocked for {SL_COOLDOWN_MINUTES}min")
            elif pnl >= cfg.take_profit:
                # Fixed-% take profit
                if is_day_trade and daytrades >= cfg.pdt_limit:
                    log.info(
                        f"{symbol:<5} | HOLD    | pnl={pnl*100:+.2f}% · "
                        "TP hit but PDT exhausted — hold overnight"
                    )
                else:
                    submit_close(cfg, trading, symbol, qty, "TAKE_PROFIT", pnl)
                    if is_day_trade:
                        daytrades += 1
            elif pnl <= cfg.stop_loss:
                # Fixed-% stop loss — always fires regardless of PDT
                submit_close(cfg, trading, symbol, qty, "STOP_LOSS", pnl, price=price)
                if is_day_trade:
                    daytrades += 1
                # Cooldown: block re-entry on this symbol for 30 min (persisted to DB)
                cooldown_until = (now_et + timedelta(minutes=SL_COOLDOWN_MINUTES)).isoformat()
                db.set_cooldown(symbol, cooldown_until, reason="STOP_LOSS")
                log.info(f"{symbol:<5} | COOLDOWN | re-entry blocked for {SL_COOLDOWN_MINUTES}min")
            else:
                fib_ctx = f" · fib382=${fib382:.2f}" if fib382 else ""
                log.info(
                    f"{symbol:<5} | HOLD    | ${price:.2f} entry=${entry:.2f} "
                    f"pnl={pnl*100:+.2f}%{fib_ctx}"
                )
            continue

        # ── ENTRY ──
        blocked: list[str] = []
        if circuit_open:
            blocked.append("circuit")
        # Entry window — only allow new entries before configured ET time
        if cfg.entry_window_end:
            ew_h, ew_m = map(int, cfg.entry_window_end.split(":"))
            entry_cutoff = now_et.replace(hour=ew_h, minute=ew_m, second=0, microsecond=0)
            if now_et >= entry_cutoff:
                blocked.append(f"entry_window({cfg.entry_window_end}ET)")
        # Reserve 1 PDT token for exits — only enter if budget allows both entry+exit
        if daytrades >= cfg.pdt_limit - 1:
            blocked.append(f"pdt({daytrades}/{cfg.pdt_limit})")
        if len(positions) >= cfg.max_positions:
            blocked.append(f"max_pos({len(positions)}/{cfg.max_positions})")
        if cash < cfg.allocation_per_stock:
            blocked.append("cash")
        # Pending order guard — skip if an unfilled buy already exists for this symbol
        if symbol in pending_buys:
            blocked.append("pending_order")
        # SL cooldown — block re-entry for 30 min after a stop-loss exit (from DB)
        if db.is_in_cooldown(symbol, now_et):
            cooldowns = db.get_active_cooldowns()
            cooldown_until_str = cooldowns.get(symbol, "")
            if cooldown_until_str:
                try:
                    cooldown_until = datetime.fromisoformat(cooldown_until_str)
                    mins_left = max(1, int((cooldown_until - now_et).total_seconds() / 60))
                    blocked.append(f"sl_cooldown({mins_left}min)")
                except ValueError:
                    pass

        # Composite signal score (0-5: VWAP, RSI, relVol, BB, Fib0.618)
        df = bars_by_symbol.get(symbol)
        sig_score, sig_detail = score_entry(symbol, price, df, cfg, fib_levels)

        if sig_score < cfg.min_signal_score:
            blocked.append(f"score={sig_score}/{cfg.min_signal_score}")

        if blocked:
            max_score = 5 if cfg.use_fib_signals else 4
            log.info(
                f"{symbol:<5} | WATCH   | ${price:.2f} day={day_chg*100:+.2f}% "
                f"score={sig_score}/{max_score} [{sig_detail}] blocked:{','.join(blocked)}"
            )
            continue

        # All checks passed — enter
        fib_ctx = f" fib618=${fib_levels[symbol]['fib618']:.2f}" if symbol in fib_levels else ""
        log.info(f"{symbol:<5} | SIGNAL  | score={sig_score}/5 [{sig_detail}]{fib_ctx}")
        # Track which data source was used for this entry signal
        bars_source = "bars" if symbol in bars_by_symbol else "fallback"
        submit_buy(cfg, trading, symbol, price, sig_score, sig_detail, bars_source=bars_source)
        cash -= cfg.allocation_per_stock


# ─────────────────────────── Entrypoint ───────────────────────────

def main() -> None:
    # ─────────────────────── Initialize Secrets & DB ───────────────────────
    log.info("🔐 Loading credentials from environment...")
    try:
        secrets = Secrets(dev_mode=True)  # dev_mode=True loads .env for local testing
    except Exception as e:
        log.critical(f"❌ Failed to load credentials: {e}")
        sys.exit(1)

    log.info("📊 Initializing database...")
    try:
        db.init_db()
    except Exception as e:
        log.critical(f"❌ Failed to initialize database: {e}")
        sys.exit(1)

    # ─────────────────────── Load Trading Config ───────────────────────
    cfg = load_config(secrets)

    log.info("═" * 65)
    log.info(f"Alpaca Bot v2 | paper={cfg.paper} | dry_run={cfg.dry_run}")
    log.info(
        f"Capital ${cfg.initial_capital:,.0f} · "
        f"${cfg.allocation_per_stock:,.0f}/pos · "
        f"TP {cfg.take_profit*100:+.1f}% · SL {cfg.stop_loss*100:+.1f}%"
    )
    max_score = 5 if cfg.use_fib_signals else 4
    fib_tag   = " · Fib0.618(±3%) · exit@0.382/0.786" if cfg.use_fib_signals else ""
    log.info(
        f"Signal  | score≥{cfg.min_signal_score}/{max_score} · "
        f"VWAP dev≤{cfg.vwap_dev_max*100:.1f}% · "
        f"RSI<{cfg.rsi_max:.0f} · "
        f"relVol≥{cfg.min_rel_volume:.1f}x · BB_lower{fib_tag}"
    )
    log.info(f"Watchlist ({len(cfg.watchlist)}): {', '.join(cfg.watchlist)}")
    if cfg.entry_window_end or cfg.force_close_time:
        log.info(
            f"Intraday | entries until {cfg.entry_window_end or 'EOD'} ET · "
            f"force-close at {cfg.force_close_time or 'disabled'} ET"
        )
    log.info("═" * 65)

    trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
    data    = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)

    if cfg.run_once:
        run_cycle(cfg, trading, data)
        return

    while True:
        try:
            run_cycle(cfg, trading, data)
        except KeyboardInterrupt:
            log.info("Interrupted. Exiting.")
            break
        except Exception as e:
            log.exception(f"Unhandled cycle error: {e}")
        time.sleep(cfg.interval_seconds)


if __name__ == "__main__":
    main()
