"""
risk_manager.py — Position Sizing and Portfolio Guards

WHY THESE NUMBERS:

  RISK_PER_TRADE_PCT = 1.5%
    Derived from Kelly-inspired thinking: with a 2.67:1 reward-to-risk ratio
    and an assumed win rate of ~40% (conservative for momentum), the Kelly
    fraction is ~10%. We use 1/7th of Kelly (1.5%) to be safe in paper testing
    before trusting the numbers. This means 6 consecutive losses = ~9% drawdown,
    which is uncomfortable but not catastrophic.

  ATR_STOP_MULTIPLIER = 2.0
    2× ATR is the minimum needed to avoid routine intraday volatility stopping
    you out. Using 1× ATR gets stopped out by normal noise ~50% of the time.
    Using 3× ATR makes position sizes too small to be meaningful. 2× is the
    standard institutional intraday stop distance.

  ATR_PROFIT_MULTIPLIER = 4.0
    4 ATR take-profit gives a 2:1 reward-to-risk ratio (4 ATR gain / 2 ATR loss).
    The actual R:R is 2.0:1. At a 35% win rate, this is EV-positive:
      EV = 0.35 × 4 − 0.65 × 2 = 1.40 − 1.30 = +0.10 per unit risked.

  MAX_POSITIONS = 5
    Diversification benefit flattens out after 5–7 uncorrelated positions.
    With 10 stocks in our universe, 5 max gives decent diversification while
    keeping the portfolio manageable. More than 5 concurrent positions on a
    $100k account means individual positions are too small to move the needle.

  MAX_POSITION_PCT = 0.20
    Hard cap: no single position can be more than 20% of the portfolio.
    This overrides the ATR-based size when the ATR is very tight (low-vol
    environments can produce deceptively large position sizes by the ATR calc).
"""

from dataclasses import dataclass
from typing import Optional


# ── Parameters ─────────────────────────────────────────────────────────────────

RISK_PER_TRADE_PCT    = 0.015   # 1.5% of portfolio per trade
ATR_STOP_MULTIPLIER   = 2.0     # stop = entry − 2×ATR
ATR_PROFIT_MULTIPLIER = 4.0     # take-profit = entry + 4×ATR
MAX_POSITIONS         = 5       # concurrent open positions
MAX_POSITION_PCT      = 0.20    # max 20% of portfolio in one position
MIN_QTY               = 1       # discard trades that size below 1 share


@dataclass
class SizeResult:
    qty: int
    stop_price: float
    take_profit_price: float
    risk_amount: float          # dollar amount risked on this trade
    position_value: float       # qty × entry_price
    risk_pct_of_portfolio: float
    sizing_method: str          # "risk_based" or "position_cap"


def can_open_position(current_position_count: int, symbol: str, held_symbols: list[str]) -> tuple[bool, str]:
    """
    Check portfolio-level guards before computing size.
    Returns (allowed, reason_string).
    """
    if symbol in held_symbols:
        return False, f"already_holding_{symbol}"

    if current_position_count >= MAX_POSITIONS:
        return False, f"max_positions_reached_{current_position_count}"

    return True, "ok"


def calculate_size(
    portfolio_value: float,
    entry_price: float,
    atr: float,
) -> Optional[SizeResult]:
    """
    Calculate position size, stop price, and take-profit price.

    Returns None if the resulting size is below MIN_QTY (e.g. ATR is
    unusually large relative to portfolio, or position cap is too tight).

    Sizing logic:
      1. Compute max dollar risk: portfolio_value × RISK_PER_TRADE_PCT
      2. Stop distance in dollars: atr × ATR_STOP_MULTIPLIER
      3. Shares by risk: risk_dollars / stop_dollars
      4. Cap by max position: (portfolio_value × MAX_POSITION_PCT) / entry_price
      5. Take the smaller of steps 3 and 4
      6. Floor to integer shares (fractional shares not used — simpler accounting)
    """
    if atr <= 0 or entry_price <= 0 or portfolio_value <= 0:
        return None

    stop_distance   = atr * ATR_STOP_MULTIPLIER
    risk_dollars    = portfolio_value * RISK_PER_TRADE_PCT
    qty_by_risk     = risk_dollars / stop_distance

    max_pos_value   = portfolio_value * MAX_POSITION_PCT
    qty_by_cap      = max_pos_value / entry_price

    raw_qty         = min(qty_by_risk, qty_by_cap)
    qty             = int(raw_qty)   # floor — never round up, never go over budget

    if qty < MIN_QTY:
        return None

    sizing_method  = "risk_based" if qty_by_risk <= qty_by_cap else "position_cap"
    stop_price     = round(entry_price - stop_distance, 2)
    take_profit    = round(entry_price + atr * ATR_PROFIT_MULTIPLIER, 2)
    position_value = round(qty * entry_price, 2)
    actual_risk    = round(qty * stop_distance, 2)

    # stop_price must be positive (guard against very small prices or huge ATR)
    if stop_price <= 0:
        return None

    return SizeResult(
        qty=qty,
        stop_price=stop_price,
        take_profit_price=take_profit,
        risk_amount=actual_risk,
        position_value=position_value,
        risk_pct_of_portfolio=round(actual_risk / portfolio_value * 100, 3),
        sizing_method=sizing_method,
    )
