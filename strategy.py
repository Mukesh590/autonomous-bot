"""
strategy.py — EMA Momentum with RSI Confirmation

WHY THIS MODULE EXISTS AS A PURE FUNCTION:
  Signal generation has no business touching the network, accounts, or orders.
  Keeping it pure makes it trivially testable and easy to reason about in isolation.
  You can replay historical bars through it without any side effects.

INDICATOR CHOICES:
  EMA(9)  — "fast" line, 9 × 15 min = ~2.25 hours of price memory.
             Responds quickly to price shifts without being as noisy as EMA(5).

  EMA(21) — "slow" line, 21 × 15 min = ~5.25 hours of price memory.
             The 9/21 pair is the standard intraday combo used on 15-min charts
             by institutional desks. Widely watched = more self-fulfilling.

  RSI(14) — 14 × 15 min = ~3.5 hours. Standard Wilder period.
             Used here as a FILTER, not a primary signal:
               - Entry: RSI must be 45–65 (momentum confirmed, not overbought)
               - Exit:  RSI > 75 forces an exit regardless of EMA state

  ATR(14) — Returned in the signal so the risk manager can compute
             volatility-adaptive stops without re-fetching data.

SIGNAL SEMANTICS:
  BUY  — open a new long position (only acted on if no position exists)
  SELL — close an existing long position
  HOLD — do nothing
"""

from dataclasses import dataclass
from typing import Literal

import pandas as pd
import ta


# ── Tuneable parameters ────────────────────────────────────────────────────────

FAST_EMA_PERIOD = 9
SLOW_EMA_PERIOD = 21
RSI_PERIOD = 14
ATR_PERIOD = 14

RSI_ENTRY_MIN = 45   # below this = weak momentum, skip the trade
RSI_ENTRY_MAX = 65   # above this = already overbought before entry
RSI_EXIT_OVERBOUGHT = 75  # force-exit when euphoria is likely priced in

# Minimum bars needed to produce reliable indicator values.
# Using 3× the longest period to ensure the EMA has fully "warmed up".
MIN_BARS_REQUIRED = SLOW_EMA_PERIOD * 3


# ── Output type ────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol: str
    signal: Literal["BUY", "SELL", "HOLD"]
    reason: str

    # Indicator snapshots — logged verbatim so every decision is auditable
    price: float
    fast_ema: float
    slow_ema: float
    rsi: float
    atr: float
    ema_spread_pct: float   # (fast - slow) / slow × 100; + = bullish spread

    # Crossover flags for the current bar
    crossed_above: bool     # fast crossed above slow THIS bar
    crossed_below: bool     # fast crossed below slow THIS bar


def generate_signal(symbol: str, bars: pd.DataFrame) -> Signal:
    """
    Generate a trading signal from a DataFrame of OHLCV bars.

    bars must have columns: open, high, low, close, volume
    bars must be sorted ascending by time (oldest first).

    Returns a Signal dataclass. Never raises — returns HOLD with
    reason="insufficient_data" if bars are too short.
    """
    if len(bars) < MIN_BARS_REQUIRED:
        return Signal(
            symbol=symbol,
            signal="HOLD",
            reason="insufficient_data",
            price=float(bars["close"].iloc[-1]) if len(bars) > 0 else 0.0,
            fast_ema=0.0, slow_ema=0.0, rsi=0.0, atr=0.0,
            ema_spread_pct=0.0, crossed_above=False, crossed_below=False,
        )

    close = bars["close"]
    high  = bars["high"]
    low   = bars["low"]

    # ── Calculate indicators ───────────────────────────────────────────────────

    fast_ema_series = ta.trend.EMAIndicator(close=close, window=FAST_EMA_PERIOD).ema_indicator()
    slow_ema_series = ta.trend.EMAIndicator(close=close, window=SLOW_EMA_PERIOD).ema_indicator()
    rsi_series      = ta.momentum.RSIIndicator(close=close, window=RSI_PERIOD).rsi()
    atr_series      = ta.volatility.AverageTrueRange(
                          high=high, low=low, close=close, window=ATR_PERIOD
                      ).average_true_range()

    # Current and previous bar values
    cur_fast = float(fast_ema_series.iloc[-1])
    cur_slow = float(slow_ema_series.iloc[-1])
    prev_fast = float(fast_ema_series.iloc[-2])
    prev_slow = float(slow_ema_series.iloc[-2])

    cur_rsi   = float(rsi_series.iloc[-1])
    cur_atr   = float(atr_series.iloc[-1])
    cur_price = float(close.iloc[-1])

    # Guard against NaN (can happen if data has gaps)
    if any(v != v for v in [cur_fast, cur_slow, prev_fast, prev_slow, cur_rsi, cur_atr]):
        return Signal(
            symbol=symbol, signal="HOLD", reason="nan_indicator",
            price=cur_price, fast_ema=cur_fast, slow_ema=cur_slow,
            rsi=cur_rsi, atr=cur_atr, ema_spread_pct=0.0,
            crossed_above=False, crossed_below=False,
        )

    # Crossover detection: did the relationship CHANGE this bar?
    crossed_above = (prev_fast <= prev_slow) and (cur_fast > cur_slow)
    crossed_below = (prev_fast >= prev_slow) and (cur_fast < cur_slow)

    ema_spread_pct = round((cur_fast - cur_slow) / cur_slow * 100, 4)

    base = dict(
        symbol=symbol,
        price=round(cur_price, 4),
        fast_ema=round(cur_fast, 4),
        slow_ema=round(cur_slow, 4),
        rsi=round(cur_rsi, 2),
        atr=round(cur_atr, 4),
        ema_spread_pct=ema_spread_pct,
        crossed_above=crossed_above,
        crossed_below=crossed_below,
    )

    # ── Signal logic ───────────────────────────────────────────────────────────
    #
    # BUY conditions (ALL must be true):
    #   1. EMA(9) just crossed ABOVE EMA(21) this bar  — trend flip confirmed
    #   2. RSI is between RSI_ENTRY_MIN and RSI_ENTRY_MAX — has momentum,
    #      not yet overbought
    #
    # WHY require a fresh crossover rather than just "fast > slow"?
    #   Entering mid-trend on a sustained crossover means you're late and
    #   chasing. A fresh crossover is the earliest reliable confirmation
    #   that a trend shift occurred. It keeps turnover low and reduces
    #   the number of "already running" trades.
    #
    # SELL conditions (first match wins):
    #   1. EMA(9) just crossed BELOW EMA(21) — trend reversal confirmed
    #   2. RSI > RSI_EXIT_OVERBOUGHT — euphoria exit regardless of EMA state
    #      (protects profit before the inevitable mean-reversion)
    #
    # Everything else is HOLD.

    if crossed_above and RSI_ENTRY_MIN <= cur_rsi <= RSI_ENTRY_MAX:
        return Signal(**base, signal="BUY", reason="ema_crossover_up_rsi_confirmed")

    if crossed_below:
        return Signal(**base, signal="SELL", reason="ema_crossover_down")

    if cur_rsi > RSI_EXIT_OVERBOUGHT:
        return Signal(**base, signal="SELL", reason="rsi_overbought_exit")

    return Signal(**base, signal="HOLD", reason="no_signal")
