"""
trade_logger.py — Dual-destination logging

WHY TWO LOGGING SYSTEMS:

  loguru (structured text log):
    Human-readable. Good for tailing in production with `railway logs`.
    Includes context (timestamps, severity, module) automatically.
    Rotated at 50 MB so the Railway volume doesn't fill up over weeks.

  CSV files (machine-readable):
    Perfect for post-session analysis in Excel/pandas/Jupyter.
    Three separate CSVs because they have different schemas and different
    query patterns:

    signals.csv  — every evaluation of every symbol every cycle, whether
                   we acted or not. This is the decision audit trail.
                   Ask: "why didn't the bot buy AAPL at 14:30?"

    trades.csv   — only order submissions. Includes fill price, stop,
                   take-profit. Ask: "what was our average R:R?"

    portfolio.csv — snapshot of account state at the start of each cycle.
                    Ask: "what was our equity curve?"

DECISION: log to ./logs/ directory (relative to CWD).
  Railway mounts a persistent volume at /data — SETUP.md explains how to
  symlink or configure LOG_DIR=/data/logs for persistence across restarts.
  Default ./logs is fine for local development.
"""

import csv
import os
from datetime import datetime
from pathlib import Path

from loguru import logger


# ── Column definitions ─────────────────────────────────────────────────────────

SIGNAL_COLUMNS = [
    "timestamp", "symbol", "signal", "reason",
    "price", "fast_ema", "slow_ema", "rsi", "atr", "ema_spread_pct",
    "crossed_above", "crossed_below",
    "action_taken",   # what did the bot actually DO with this signal?
]

TRADE_COLUMNS = [
    "timestamp", "symbol", "action", "qty",
    "entry_price", "stop_price", "take_profit_price",
    "risk_amount", "position_value", "risk_pct_of_portfolio",
    "sizing_method", "order_id",
    "fast_ema", "slow_ema", "rsi", "atr",
    "portfolio_value_at_entry",
]

PORTFOLIO_COLUMNS = [
    "timestamp", "portfolio_value", "cash", "equity",
    "open_positions", "position_symbols",
]


class TradeLogger:
    def __init__(self, log_dir: str | None = None):
        self.log_dir = Path(log_dir or os.getenv("LOG_DIR", "logs"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._setup_loguru()
        self._setup_csv(self.log_dir / "signals.csv",   SIGNAL_COLUMNS)
        self._setup_csv(self.log_dir / "trades.csv",    TRADE_COLUMNS)
        self._setup_csv(self.log_dir / "portfolio.csv", PORTFOLIO_COLUMNS)

        logger.info(f"TradeLogger initialised. Log directory: {self.log_dir.resolve()}")

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _setup_loguru(self):
        log_file = self.log_dir / "bot.log"
        logger.add(
            str(log_file),
            rotation="50 MB",
            retention="30 days",
            compression="gz",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | {message}",
            enqueue=True,   # thread-safe writes
        )
        logger.info(f"File logging started at {log_file}")

    def _setup_csv(self, path: Path, columns: list[str]):
        """Write header row only if the file doesn't already exist (resumable across restarts)."""
        if not path.exists():
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()

    def _append_csv(self, path: Path, columns: list[str], row: dict):
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writerow({"timestamp": datetime.utcnow().isoformat(), **row})

    # ── Public API ─────────────────────────────────────────────────────────────

    def log_signal(self, signal_data: dict, action_taken: str):
        """
        Log every signal evaluation — BUY, SELL, or HOLD.
        action_taken describes what the bot did: "order_submitted", "skipped_max_positions",
        "no_position_to_close", "hold", etc.
        """
        row = {**signal_data, "action_taken": action_taken}
        self._append_csv(self.log_dir / "signals.csv", SIGNAL_COLUMNS, row)

        logger.info(
            f"SIGNAL | {signal_data.get('symbol','?'):6s} | "
            f"{signal_data.get('signal','?'):4s} | "
            f"reason={signal_data.get('reason','?'):<35s} | "
            f"price={signal_data.get('price',0):.2f} | "
            f"rsi={signal_data.get('rsi',0):.1f} | "
            f"ema_spread={signal_data.get('ema_spread_pct',0):+.3f}% | "
            f"action={action_taken}"
        )

    def log_trade(self, trade_data: dict):
        """
        Log every order submission.
        trade_data should contain all TRADE_COLUMNS fields.
        """
        self._append_csv(self.log_dir / "trades.csv", TRADE_COLUMNS, trade_data)

        action = trade_data.get("action", "?")
        symbol = trade_data.get("symbol", "?")
        qty    = trade_data.get("qty", 0)
        price  = trade_data.get("entry_price", 0)
        stop   = trade_data.get("stop_price", 0)
        tp     = trade_data.get("take_profit_price", 0)
        risk   = trade_data.get("risk_amount", 0)
        oid    = trade_data.get("order_id", "?")

        if action == "BUY":
            logger.success(
                f"TRADE  | {symbol:6s} | BUY  {qty} shares @ ${price:.2f} | "
                f"stop=${stop:.2f} | tp=${tp:.2f} | risk=${risk:.2f} | order={oid}"
            )
        elif action == "SELL":
            logger.success(
                f"TRADE  | {symbol:6s} | SELL {qty} shares @ ${price:.2f} | "
                f"reason={trade_data.get('reason','?')} | order={oid}"
            )
        else:
            logger.info(f"TRADE  | {symbol:6s} | {action} | {trade_data}")

    def log_portfolio(self, portfolio_data: dict):
        """Snapshot of account state at the start of each cycle."""
        self._append_csv(self.log_dir / "portfolio.csv", PORTFOLIO_COLUMNS, portfolio_data)

        logger.info(
            f"PORTFOLIO | equity=${portfolio_data.get('equity', 0):,.2f} | "
            f"cash=${portfolio_data.get('cash', 0):,.2f} | "
            f"positions={portfolio_data.get('open_positions', 0)} "
            f"({portfolio_data.get('position_symbols', '')})"
        )

    def log_cycle_start(self, cycle_num: int):
        logger.info(f"{'─'*60}")
        logger.info(f"CYCLE #{cycle_num} — {datetime.utcnow().isoformat()} UTC")

    def log_error(self, context: str, error: Exception):
        logger.exception(f"ERROR in {context}: {error}")

    def log_info(self, message: str):
        logger.info(message)

    def log_warning(self, message: str):
        logger.warning(message)
