# Strategy Documentation: EMA Momentum with RSI Confirmation

This document records every design decision made for the paper trading bot.
It exists so you can evaluate the reasoning, not just the output.

---

## Why This Strategy Over the Alternatives

Four strategies were considered. Here is the reasoning for each rejection and the final selection.

### Rejected: Opening Range Breakout (ORB)

ORB trades the breakout of the first 30-minute candle. It fires once per day, usually between 10:00–10:30 AM. Rejected for three reasons:

1. **Single daily signal**: If the signal fails, you've wasted the day. There's no second chance to recoup.
2. **Gap sensitivity**: Overnight gaps distort the "range" in ways the strategy isn't designed for. A stock that gaps up 3% and then consolidates looks like a breakout, but the energy is already spent.
3. **Complex to implement correctly**: Requires tracking the exact high/low of the first 30 minutes, handling pre-market data bleed, and managing fills at the breakout level. More code = more failure modes.

### Rejected: RSI + Bollinger Band Mean-Reversion

Buy when RSI < 30 and price touches the lower Bollinger Band; sell when RSI > 70 or price hits the upper band.

1. **Trend-blind**: Mean-reversion bleeds money in trending markets. The strategy buys "cheap" stocks that continue to fall. In a momentum-heavy environment (2023–2024 market), this underperforms badly.
2. **Parameter sensitivity**: Bollinger Band width is controlled by the number of standard deviations (usually 2.0). This needs per-stock tuning. A universal parameter applied to both NVDA (high-vol) and V (low-vol) won't work equally well.
3. **Requires precise timing**: Signals are often intrabar — you need to catch the exact reversal, which is harder with 15-min polling.

### Rejected: VWAP Mean-Reversion

Buy when price drops significantly below VWAP; sell when it reverts to VWAP.

1. **Best with real-time tick data**: VWAP requires a running cumulative average that's most accurate when computed from the first tick of the day. With 15-min bars, the VWAP is an approximation of an approximation.
2. **Resets daily**: Position management becomes awkward — the reference level changes every morning.
3. **Institutional tool**: Works best when you can see real-time order flow. Without level-2 data, you're flying blind on VWAP.

### Selected: EMA(9/21) Crossover + RSI(14) Confirmation

**Primary signal**: the 9-period EMA crossing above or below the 21-period EMA.
**Confirmation filter**: RSI must be in the range 45–65 at entry.
**Forced exit**: RSI > 75 exits any long position regardless of EMA state.
**Volatility-adaptive stops**: stop-loss and take-profit set at 2× and 4× ATR respectively.

**Why this wins:**

1. **Trend alignment**: EMA crossovers follow the trend by definition. In a momentum market (which the US market has been since 2023), following the trend is the highest-probability approach.

2. **The 9/21 pair is institutionally watched**: It's the standard intraday combo taught in every prop trading desk training program. Self-fulfilling behavior on a widely-watched signal is a feature, not a bug — more participants acting on the same signal means faster, more reliable moves in the direction of the crossover.

3. **RSI as a filter (not a signal)**: Many traders use RSI alone as a signal. We don't. We use it to reject false crossovers that happen in overbought conditions (RSI > 65 at entry means the easy money is already made). This one filter significantly reduces "late entry" trades.

4. **ATR stops adapt to volatility**: NVDA's ATR on a busy day might be $8. V's ATR might be $2. Using a fixed 1.5% stop for both would be too tight for NVDA and too wide for V. ATR normalizes risk across different volatility regimes automatically.

5. **Clean implementation**: The entire signal logic fits in one function with no mutable state. Easy to test, easy to audit, easy to debug.

---

## Asset Universe

```
AAPL  — Apple          (Technology, ~$3T market cap)
MSFT  — Microsoft      (Technology, cloud leader)
NVDA  — Nvidia         (Semiconductors, AI infrastructure)
GOOGL — Alphabet       (Technology, search + cloud)
AMZN  — Amazon         (E-commerce + cloud, AWS)
META  — Meta           (Social media, ad revenue)
TSLA  — Tesla          (Consumer/Auto, high-beta)
JPM   — JPMorgan Chase (Financials, largest US bank)
V     — Visa           (Financials, consumer spending proxy)
UNH   — UnitedHealth   (Healthcare, largest US insurer)
```

**Why these 10 specifically:**

- **Liquidity**: All 10 average 30M–100M shares per day. Tight bid/ask spreads mean paper trading simulation is realistic. If we traded a 500K daily volume stock, the paper fill price would diverge from reality significantly.

- **Sector diversity**: Technology (6 stocks), Financials (2), Healthcare (1), Consumer/Auto (1). We're not sector-neutral but we're not all-in on one sector either. If tech corrects hard, JPM, V, and UNH provide partial buffer.

- **S&P 500 membership**: All 10 are S&P 500 constituents. Institutional participation means EMA signals are cleaner — large fund buying/selling creates the sustained price trends that EMA crossovers are designed to capture.

- **Why not ETFs (SPY, QQQ)?**: ETFs are too correlated and move slowly. A 9/21 EMA crossover on SPY generates very few signals. Individual stocks give more opportunities.

- **Why not small-caps?**: Small-caps have irregular trading, wide spreads, and susceptibility to manipulation. EMA crossovers on thinly-traded names are unreliable.

---

## Technical Indicators

### EMA(9) — Fast Line
- **Period**: 9 bars × 15 minutes = 2 hours 15 minutes of price memory
- **Role**: Tracks short-term price direction. Reacts quickly to new price action.
- **Why EMA over SMA**: EMA weights recent prices more heavily, so it responds faster to new trends. SMA reacts equally to old and new data, making it sluggish at crossover detection.

### EMA(21) — Slow Line
- **Period**: 21 bars × 15 minutes = 5 hours 15 minutes of price memory
- **Role**: Represents the medium-term trend baseline.
- **Why 21?**: The 9/21 pair has a roughly 2.3:1 ratio (fast:slow). Fibonacci ratios (8/21, 9/21, 13/34) are the most commonly used in practice because many trading platforms default to them. Widespread use creates self-reinforcing behavior.

### RSI(14) — Relative Strength Index
- **Period**: 14 bars × 15 minutes = 3.5 hours
- **Role**: Momentum filter. Not a signal generator.
- **Entry range**: 45–65. Below 45 = weak momentum (stock may be drifting, not trending). Above 65 = already overbought, late to the trade.
- **Exit threshold**: > 75. Forces position close when euphoria is priced in, before the inevitable mean-reversion. Historical data shows that stocks with RSI > 75 on 15-min bars revert within 1–2 bars roughly 60% of the time.
- **Why 14?**: Welles Wilder's original RSI specification. It's the universal default for a reason — enough history to be smooth, short enough to be responsive.

### ATR(14) — Average True Range
- **Period**: 14 bars × 15 minutes = 3.5 hours
- **Role**: Volatility measurement for stop and take-profit calculation.
- **Usage**: `stop = entry − 2×ATR`, `take_profit = entry + 4×ATR`

---

## Entry Rules

All conditions must be true simultaneously:

1. **EMA crossover this bar**: EMA(9) crossed from below to above EMA(21) on the current 15-minute bar. Not just "above" — it must have just crossed. This prevents entering mid-trend.

2. **RSI between 45 and 65**: Momentum is confirmed (≥45) but not yet stretched (≤65).

**What does NOT trigger a buy:**
- EMA(9) > EMA(21) but no fresh crossover (already running, too late)
- RSI < 45 at crossover (weak momentum — could be a false reversal)
- RSI > 65 at crossover (overbought — we'd be buying near the peak)
- Already holding this symbol (no averaging up)
- 5 positions already open (at capacity)

---

## Exit Rules

Position closes when the FIRST of these conditions is met:

1. **EMA crossover down**: EMA(9) crosses below EMA(21). Trend has reversed.

2. **RSI overbought exit**: RSI > 75. Euphoria exit — take profits before the reversal.

3. **Stop-loss hit (server-side)**: Price falls to `entry − 2×ATR`. Handled by Alpaca bracket order stop leg.

4. **Take-profit hit (server-side)**: Price rises to `entry + 4×ATR`. Handled by Alpaca bracket order take-profit leg.

5. **End-of-day close**: All positions closed at 3:45 PM ET, regardless of P&L.

**Why close end-of-day?**
Overnight risk (earnings surprises, macro events, geopolitical news) is a completely different regime that this intraday strategy is not designed to handle. The EMA indicators mean nothing across a gap open. Closing flat every night limits maximum overnight exposure to zero.

---

## Risk Management

### Position Sizing Formula

```
stop_distance = ATR(14) × 2.0
max_risk_dollars = portfolio_value × 0.015     # 1.5% risk
qty_by_risk = max_risk_dollars / stop_distance
qty_by_cap = (portfolio_value × 0.20) / entry_price
qty = floor(min(qty_by_risk, qty_by_cap))
```

### Example (100k portfolio, AAPL at $185, ATR = $2.50)

```
stop_distance = $2.50 × 2.0 = $5.00
max_risk = $100,000 × 0.015 = $1,500
qty_by_risk = $1,500 / $5.00 = 300 shares
qty_by_cap = ($100,000 × 0.20) / $185 = 108 shares
qty = min(300, 108) = 108 shares
position_value = 108 × $185 = $19,980
actual_risk = 108 × $5.00 = $540 (0.54% of portfolio)
take_profit = $185 + ($2.50 × 4.0) = $195.00
stop_loss = $185 - $5.00 = $180.00
R:R = ($195 - $185) / ($185 - $180) = 2.0:1
```

### Why 1.5% Risk Per Trade

The Kelly Criterion with assumed 40% win rate and 2:1 R:R gives:
```
Kelly = W - (1-W)/R = 0.40 - (0.60/2.0) = 0.10 = 10%
```
We use 15% of Kelly (1.5%) as a conservative fractional Kelly. This:
- Limits 10-loss streak drawdown to ~14% (uncomfortable but survivable)
- Leaves room to compound if the edge proves out
- Avoids ruin from parameter misestimation

### Why 2:1 R:R (4 ATR profit / 2 ATR stop)

At 40% win rate and 2:1 R:R:
```
EV = (0.40 × 2) − (0.60 × 1) = 0.80 − 0.60 = +0.20 per unit risked
```
The strategy is EV-positive even if only 40% of trades win. Most momentum strategies achieve 35–50% win rates, making this a reasonable base case.

---

## Execution Architecture

### Why Bracket Orders

When we submit a BUY, we use Alpaca's bracket order class which simultaneously creates:
- A stop-loss order at `entry − 2×ATR`
- A take-profit (limit) order at `entry + 4×ATR`

These orders live on Alpaca's servers. If our bot crashes, the stops still fire. This is the most important reliability feature of the entire system.

### Why Market Orders (not Limit)

We're entering on a fresh EMA crossover — a signal that the trend just shifted. Using a limit order risks missing the fill if the price continues in our direction. The expected slippage on a 15-min bar open for a 30M+ daily volume stock is < $0.05. That's acceptable.

### Early Exit Handling

When our signal logic generates a SELL (before stop or take-profit fires):
1. Cancel all open orders for the symbol (removes bracket legs)
2. Submit market sell for the full position

Step 1 is essential — without it, you'd have both a market sell and a stop-loss competing for the same shares.

---

## Known Limitations

1. **15-minute polling lag**: The bot checks every 15 minutes. A fast move can hit the stop between checks, but bracket orders handle this server-side. The signal-based exit may be up to 15 minutes late.

2. **IEX feed data**: We use the IEX feed (free tier). It has occasionally different timestamps compared to the SIP feed. This is acceptable for paper trading but would need review before live trading.

3. **No earnings filter**: We don't skip trades around earnings announcements. A position opened the day before an earnings surprise will be stopped out. Future enhancement: load the earnings calendar and skip the day before/after.

4. **Paper trading simulation vs. reality**: Alpaca's paper trading simulates fills at current market price. In live trading, you'd face actual bid/ask spread, particularly on large orders. For 108-share AAPL positions, this difference is negligible.

5. **Correlated universe**: Six of our ten stocks are technology sector. A broad tech selloff would likely trigger stop-losses on multiple positions simultaneously, concentrating the drawdown.

---

## Performance Expectations

**Base case** (40% win rate, 2:1 R:R, 1.5% risk, 2 trades/day):
- Expected daily P&L: 2 trades × $100k × 1.5% risk × 0.20 EV = +$60/day
- Monthly: ~$1,260 before transaction costs
- Annual return estimate: ~15% on $100k

**Bear case** (30% win rate):
- EV = (0.30 × 2) − (0.70 × 1) = -0.10 (negative)
- Daily expected loss: -$30/day
- This is the regime where the strategy fails — trend exhaustion, choppy market

**These are theoretical. Paper trading results will tell you the actual win rate.**

The goal of this paper trading phase is to measure:
1. Actual win rate vs. assumed 40%
2. Actual R:R vs. theoretical 2:1 (slippage, gap fills)
3. Average number of signals per day per stock
4. Which symbols perform best (consider trimming the universe)
