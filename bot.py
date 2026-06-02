"""
bot.py — Main Orchestrator

ARCHITECTURE DECISIONS:

  Single process, single thread:
    The bot fetches data and places orders synchronously. There is no need
    for async here — each 15-minute cycle takes < 5 seconds (10 stocks ×
    ~0.3s REST call). Async would add complexity for zero benefit at this scale.

  schedule library instead of cron:
    cron would require a cron daemon (adds Docker complexity). The schedule
    library runs inside the Python process itself — simpler, portable, works
    identically on Railway and locally.

  One broker client, one data client:
    alpaca-py separates trading (account/orders/positions) from data (bars).
    This is intentional — the data feed and the brokerage are separate products
    on Alpaca's side. We initialise both once at startup to avoid re-auth on
    every cycle.

  Market hours guard:
    We run 9:45–15:45 ET (weekdays). The 15-minute buffer from open/close
    avoids opening auction spikes and end-of-day basket trading noise.
    The schedule still fires every 15 minutes outside those hours — it just
    immediately returns after the hours check. This keeps the timing logic
    simple and in one place.

  Bracket orders for entries:
    When we BUY, we simultaneously set a stop-loss and take-profit via
    Alpaca bracket orders. This means stops are enforced server-side even if
    our bot crashes or loses connectivity. The bot's signal loop then handles
    the SELL signal (EMA crossover / RSI overbought) as an early exit by
    cancelling the bracket legs and closing the position at market.

  No state files / no database:
    All position state comes from Alpaca's API. If the bot restarts, it
    re-queries positions and resumes managing them. This makes the bot
    stateless and easy to restart without data inconsistency.

UNIVERSE RATIONALE (see STRATEGY.md for full detail):
  10 high-liquidity large-caps across 5 sectors. High volume = tight bid/ask
  spread even in paper trading simulation. Well-followed names = EMA signals
  are cleaner (less manipulation, more institutional participation).
"""

import os
import sys
import signal as os_signal
import time
from datetime import datetime, time as dt_time, timedelta

import pandas as pd
import pytz
import schedule
from dotenv import load_dotenv
from loguru import logger

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.common.exceptions import APIError

from strategy import generate_signal
from risk_manager import can_open_position, calculate_size
from trade_logger import TradeLogger


# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Trading universe
# WHY THESE 10: see STRATEGY.md §Asset Universe
UNIVERSE = [
    "AAPL",   # Apple          — Tech, $3T+ market cap, 60M+ daily vol
    "MSFT",   # Microsoft      — Tech, cloud growth driver
    "NVDA",   # Nvidia         — Semiconductors, AI trade
    "GOOGL",  # Alphabet       — Tech/Ads, S&P liquidity
    "AMZN",   # Amazon         — E-commerce/Cloud
    "META",   # Meta           — Social media, ad revenue proxy
    "TSLA",   # Tesla          — High-beta, trendy, good for momentum
    "JPM",    # JPMorgan       — Financials sector exposure
    "V",      # Visa           — Consumer spending proxy
    "UNH",    # UnitedHealth   — Healthcare sector exposure
]

# Market hours (Eastern Time)
MARKET_OPEN_H,  MARKET_OPEN_M  = 9,  45   # 9:45 AM ET (15-min buffer from 9:30 open)
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 45   # 3:45 PM ET (15-min buffer from 4:00 close)

# How many 15-min bars to fetch for indicator calculation
# strategy.py needs SLOW_EMA × 3 = 63 bars minimum; fetch 100 for safety
BARS_TO_FETCH = 100

ET = pytz.timezone("US/Eastern")


# ── Global state ───────────────────────────────────────────────────────────────

trade_logger: TradeLogger | None = None
trading_client: TradingClient | None = None
data_client: StockHistoricalDataClient | None = None
cycle_count = 0
shutdown_requested = False


# ── Market hours ───────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """True if right now is within our trading window (weekdays, 9:45–15:45 ET)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:   # Saturday = 5, Sunday = 6
        return False
    open_time  = dt_time(MARKET_OPEN_H,  MARKET_OPEN_M)
    close_time = dt_time(MARKET_CLOSE_H, MARKET_CLOSE_M)
    return open_time <= now.time() <= close_time


def next_market_open() -> str:
    """Human-readable string of when the next trading window starts."""
    now = datetime.now(ET)
    # Find next weekday
    days_ahead = 1
    while True:
        candidate = now + timedelta(days=days_ahead)
        if candidate.weekday() < 5:
            return candidate.strftime(f"%Y-%m-%d {MARKET_OPEN_H:02d}:{MARKET_OPEN_M:02d} ET")
        days_ahead += 1


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_bars(symbol: str, n_bars: int = BARS_TO_FETCH) -> "pd.DataFrame | None":
    """
    Fetch the last n_bars of 15-minute bars for symbol.
    Returns a DataFrame with columns [open, high, low, close, volume]
    sorted ascending, or None on error.
    """
    try:
        end   = datetime.now(pytz.UTC)
        # Fetch extra days to account for weekends/holidays
        start = end - timedelta(days=10)

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed="iex",   # IEX feed: free tier, reliable for paper trading
        )
        bars_response = data_client.get_stock_bars(req)

        if not bars_response or symbol not in bars_response.data:
            trade_logger.log_warning(f"No bars returned for {symbol}")
            return None

        # bars_response.df is a MultiIndex (symbol, timestamp) DataFrame
        df = bars_response.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0)   # drop the symbol level

        df = df.sort_index()
        df.columns = [c.lower() for c in df.columns]

        # Keep only the columns the strategy needs
        df = df[["open", "high", "low", "close", "volume"]].copy()

        # Return the last n_bars rows
        return df.tail(n_bars)

    except Exception as e:
        trade_logger.log_error(f"fetch_bars({symbol})", e)
        return None


# ── Account helpers ────────────────────────────────────────────────────────────

def get_account_snapshot() -> dict:
    """Return portfolio_value, cash, equity and held symbols."""
    account   = trading_client.get_account()
    positions = trading_client.get_all_positions()

    held_symbols   = [p.symbol for p in positions]
    portfolio_val  = float(account.portfolio_value)
    cash           = float(account.cash)
    equity         = float(account.equity)

    return {
        "portfolio_value": portfolio_val,
        "cash":            cash,
        "equity":          equity,
        "open_positions":  len(positions),
        "position_symbols": ",".join(held_symbols),
        "held_symbols":    held_symbols,
        "positions":       {p.symbol: p for p in positions},
    }


# ── Order execution ────────────────────────────────────────────────────────────

def submit_buy(symbol: str, size_result, signal, portfolio_value: float) -> str | None:
    """
    Submit a bracket buy order (market entry + stop-loss + take-profit).

    WHY BRACKET ORDERS:
      Stop-loss and take-profit legs are held server-side by Alpaca. If our
      bot crashes, the stops still fire. This is critical for paper trading
      safety — we don't want runaway losses while the bot is restarting.

    WHY MARKET ORDER (not limit):
      Limit orders can miss fills if the price ticks away. On 15-min bars
      we're entering at the START of a new trend — slight slippage is
      acceptable. For paper trading the fill is simulated at current price.
    """
    try:
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=size_result.qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            stop_loss={"stop_price": str(size_result.stop_price)},
            take_profit={"limit_price": str(size_result.take_profit_price)},
        )
        order = trading_client.submit_order(order_data)

        trade_logger.log_trade({
            "symbol":                 symbol,
            "action":                 "BUY",
            "qty":                    size_result.qty,
            "entry_price":            signal.price,
            "stop_price":             size_result.stop_price,
            "take_profit_price":      size_result.take_profit_price,
            "risk_amount":            size_result.risk_amount,
            "position_value":         size_result.position_value,
            "risk_pct_of_portfolio":  size_result.risk_pct_of_portfolio,
            "sizing_method":          size_result.sizing_method,
            "order_id":               str(order.id),
            "fast_ema":               signal.fast_ema,
            "slow_ema":               signal.slow_ema,
            "rsi":                    signal.rsi,
            "atr":                    signal.atr,
            "portfolio_value_at_entry": portfolio_value,
        })

        return str(order.id)

    except APIError as e:
        trade_logger.log_error(f"submit_buy({symbol})", e)
        return None
    except Exception as e:
        trade_logger.log_error(f"submit_buy({symbol}) unexpected", e)
        return None


def submit_sell(symbol: str, signal, qty: int) -> str | None:
    """
    Close an existing position at market.

    Steps:
      1. Cancel all open orders for this symbol (cancels bracket legs).
      2. Submit market sell for the full position.

    WHY CANCEL FIRST:
      If a bracket stop-loss or take-profit is sitting open, submitting
      a market sell on top of it creates a conflicting order. Cancelling
      first ensures a clean close.
    """
    try:
        # Step 1: cancel open orders for this symbol
        open_orders = trading_client.get_orders(
            GetOrdersRequest(symbols=[symbol], status=QueryOrderStatus.OPEN)
        )
        for order in open_orders:
            try:
                trading_client.cancel_order_by_id(order.id)
                trade_logger.log_info(f"Cancelled order {order.id} for {symbol} before sell")
            except Exception:
                pass   # best-effort cancel; continue to close position

        # Step 2: close position
        close_resp = trading_client.close_position(symbol)

        order_id = str(close_resp.id) if hasattr(close_resp, "id") else "market_close"

        trade_logger.log_trade({
            "symbol":        symbol,
            "action":        "SELL",
            "qty":           qty,
            "entry_price":   signal.price,
            "stop_price":    0,
            "take_profit_price": 0,
            "risk_amount":   0,
            "position_value": 0,
            "risk_pct_of_portfolio": 0,
            "sizing_method": "exit",
            "order_id":      order_id,
            "fast_ema":      signal.fast_ema,
            "slow_ema":      signal.slow_ema,
            "rsi":           signal.rsi,
            "atr":           signal.atr,
            "portfolio_value_at_entry": 0,
            "reason":        signal.reason,
        })

        return order_id

    except APIError as e:
        trade_logger.log_error(f"submit_sell({symbol})", e)
        return None
    except Exception as e:
        trade_logger.log_error(f"submit_sell({symbol}) unexpected", e)
        return None


# ── Main trading cycle ─────────────────────────────────────────────────────────

def trading_cycle():
    """
    Core logic executed every 15 minutes during market hours.

    Flow per symbol:
      1. Fetch 15-min OHLCV bars
      2. Generate signal (EMA crossover + RSI)
      3. If BUY signal → check portfolio guards → size position → submit bracket order
      4. If SELL signal → cancel brackets → close position at market
      5. Log everything regardless of action taken
    """
    global cycle_count

    if not is_market_hours():
        logger.debug(f"Outside market hours. Next window: {next_market_open()}")
        return

    cycle_count += 1
    trade_logger.log_cycle_start(cycle_count)

    # ── 1. Snapshot account state ──────────────────────────────────────────────
    try:
        account = get_account_snapshot()
    except Exception as e:
        trade_logger.log_error("get_account_snapshot", e)
        return

    trade_logger.log_portfolio({
        "portfolio_value": account["portfolio_value"],
        "cash":            account["cash"],
        "equity":          account["equity"],
        "open_positions":  account["open_positions"],
        "position_symbols": account["position_symbols"],
    })

    # ── 2. Evaluate each symbol ────────────────────────────────────────────────
    for symbol in UNIVERSE:
        try:
            _evaluate_symbol(symbol, account)
        except Exception as e:
            trade_logger.log_error(f"_evaluate_symbol({symbol})", e)
            # Continue to next symbol — don't let one bad symbol crash the cycle


def _evaluate_symbol(symbol: str, account: dict):
    """Evaluate one symbol within a trading cycle."""

    bars = fetch_bars(symbol)
    if bars is None or bars.empty:
        trade_logger.log_signal(
            {"symbol": symbol, "signal": "HOLD", "reason": "no_data",
             "price": 0, "fast_ema": 0, "slow_ema": 0, "rsi": 0,
             "atr": 0, "ema_spread_pct": 0, "crossed_above": False, "crossed_below": False},
            action_taken="skipped_no_data"
        )
        return

    signal = generate_signal(symbol, bars)
    signal_dict = {
        "symbol":        signal.symbol,
        "signal":        signal.signal,
        "reason":        signal.reason,
        "price":         signal.price,
        "fast_ema":      signal.fast_ema,
        "slow_ema":      signal.slow_ema,
        "rsi":           signal.rsi,
        "atr":           signal.atr,
        "ema_spread_pct": signal.ema_spread_pct,
        "crossed_above": signal.crossed_above,
        "crossed_below": signal.crossed_below,
    }

    held_symbols = account["held_symbols"]
    positions    = account["positions"]

    # ── BUY path ───────────────────────────────────────────────────────────────
    if signal.signal == "BUY":
        allowed, guard_reason = can_open_position(
            current_position_count=account["open_positions"],
            symbol=symbol,
            held_symbols=held_symbols,
        )
        if not allowed:
            trade_logger.log_signal(signal_dict, action_taken=f"skipped_{guard_reason}")
            return

        size = calculate_size(
            portfolio_value=account["portfolio_value"],
            entry_price=signal.price,
            atr=signal.atr,
        )
        if size is None:
            trade_logger.log_signal(signal_dict, action_taken="skipped_size_too_small")
            return

        order_id = submit_buy(symbol, size, signal, account["portfolio_value"])
        action = "order_submitted" if order_id else "order_failed"
        trade_logger.log_signal(signal_dict, action_taken=action)

        # Update local state so subsequent symbols in the same cycle see this position
        if order_id:
            account["open_positions"] += 1
            account["held_symbols"].append(symbol)

    # ── SELL path ──────────────────────────────────────────────────────────────
    elif signal.signal == "SELL":
        if symbol not in held_symbols:
            trade_logger.log_signal(signal_dict, action_taken="no_position_to_close")
            return

        position = positions[symbol]
        qty = int(float(position.qty))

        order_id = submit_sell(symbol, signal, qty)
        action = "position_closed" if order_id else "close_failed"
        trade_logger.log_signal(signal_dict, action_taken=action)

        if order_id:
            account["open_positions"] = max(0, account["open_positions"] - 1)
            account["held_symbols"].remove(symbol)

    # ── HOLD path ──────────────────────────────────────────────────────────────
    else:
        trade_logger.log_signal(signal_dict, action_taken="hold")


# ── End-of-day close ───────────────────────────────────────────────────────────

def end_of_day_close():
    """
    Close all positions at 3:45 PM ET.

    WHY:
      We don't hold overnight. Overnight gaps are a different risk regime
      (earnings, macro news, geopolitical events) that our intraday EMA
      strategy is not designed to handle. Closing flat every day limits
      maximum daily loss to our worst-case of 5 positions × 2 ATR each.

    Returns schedule.CancelJob once past close time so the job unschedules
    itself and doesn't keep firing all evening.
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return

    close_time = dt_time(MARKET_CLOSE_H, MARKET_CLOSE_M)
    if not (now.time() >= close_time):
        return

    try:
        positions = trading_client.get_all_positions()
        if not positions:
            trade_logger.log_info("END-OF-DAY: no open positions — nothing to close")
            return schedule.CancelJob

        trade_logger.log_info("END-OF-DAY: closing all positions")
        for pos in positions:
            try:
                # Skip positions held in pending orders (qty_available=0 causes 403)
                if float(pos.qty_available) <= 0:
                    trade_logger.log_info(
                        f"EOD skip {pos.symbol}: qty_available=0 (held_for_orders)"
                    )
                    continue
                # Cancel open orders first
                open_orders = trading_client.get_orders(
                    GetOrdersRequest(symbols=[pos.symbol], status=QueryOrderStatus.OPEN)
                )
                for order in open_orders:
                    trading_client.cancel_order_by_id(order.id)
                # Close position
                trading_client.close_position(pos.symbol)
                trade_logger.log_info(f"EOD closed: {pos.symbol}")
            except Exception as e:
                trade_logger.log_error(f"EOD close {pos.symbol}", e)
    except Exception as e:
        trade_logger.log_error("end_of_day_close", e)

    return schedule.CancelJob  # Unschedule after running — don't fire all night


# ── Startup ────────────────────────────────────────────────────────────────────

def initialise_clients():
    global trading_client, data_client

    # paper=True tells alpaca-py to use paper-api.alpaca.markets
    trading_client = TradingClient(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        paper=True,
    )
    data_client = StockHistoricalDataClient(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
    )

    # Verify connectivity
    account = trading_client.get_account()
    logger.info(
        f"Connected to Alpaca paper account. "
        f"Portfolio value: ${float(account.portfolio_value):,.2f} | "
        f"Status: {account.status}"
    )


def handle_shutdown(signum, frame):
    global shutdown_requested
    logger.warning(f"Received signal {signum} — requesting graceful shutdown")
    shutdown_requested = True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global trade_logger

    logger.remove()   # Remove default stderr handler
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    trade_logger = TradeLogger()

    logger.info("=" * 60)
    logger.info("Alpaca Paper Trading Bot — EMA Momentum Strategy")
    logger.info(f"Universe: {', '.join(UNIVERSE)}")
    logger.info(f"Trade window: {MARKET_OPEN_H:02d}:{MARKET_OPEN_M:02d}–"
                f"{MARKET_CLOSE_H:02d}:{MARKET_CLOSE_M:02d} ET (weekdays)")
    logger.info("=" * 60)

    # Graceful shutdown on SIGTERM/SIGINT (Docker stop, Ctrl+C)
    os_signal.signal(os_signal.SIGTERM, handle_shutdown)
    os_signal.signal(os_signal.SIGINT,  handle_shutdown)

    initialise_clients()

    # Run once immediately if we're in market hours
    if is_market_hours():
        logger.info("Market is open — running initial cycle immediately")
        trading_cycle()
    else:
        logger.info(f"Market is closed. Next window: {next_market_open()}")

    # Schedule: every 15 minutes
    schedule.every(15).minutes.do(trading_cycle)
    # Also schedule EOD check every minute in the last 30 minutes of the session
    schedule.every(1).minutes.do(end_of_day_close)

    logger.info("Scheduler started. Running every 15 minutes.")

    while not shutdown_requested:
        schedule.run_pending()
        time.sleep(10)   # poll scheduler every 10 seconds

    logger.info("Shutdown requested. Exiting cleanly.")
    sys.exit(0)


if __name__ == "__main__":
    main()
