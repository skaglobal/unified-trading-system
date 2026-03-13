"""
Paper Trade Logger
===================
Records paper-trade events (signals, suggested entries, stops, targets)
to a timestamped CSV log.  In backtesting mode, outcomes are appended
when the engine resolves the trade.

Schema
------
timestamp, ticker, mode, long_score, short_score, confidence_label,
signal_type, suggested_entry, stop_loss, target1, target2, reward_risk,
atr, rsi14, vwap, rel_volume, outcome_pnl, outcome_note

DISCLAIMER: For educational decision support only.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("intraday.paper_trade_logger")

_DEFAULT_LOG_DIR = Path(__file__).parent.parent / "logs" / "paper_trades"


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

@dataclass
class PaperTradeEvent:
    """One row in the paper-trade log."""

    timestamp: str = ""
    ticker: str = ""
    mode: str = "live"          # "live" | "backtest"

    long_score: float = 0.0
    short_score: float = 0.0
    confidence_label: str = ""
    signal_type: str = ""       # AlertType value or "NONE"

    suggested_entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target1: Optional[float] = None
    target2: Optional[float] = None
    reward_risk: Optional[float] = None

    atr: Optional[float] = None
    rsi14: Optional[float] = None
    vwap: Optional[float] = None
    rel_volume: Optional[float] = None

    # Filled in after trade resolves (backtest only)
    outcome_pnl: Optional[float] = None
    outcome_note: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class PaperTradeLogger:
    """Append paper-trade events to a CSV log file.

    Args:
        log_dir:  Directory where log files are stored.
                  Defaults to ``logs/paper_trades/`` under the project root.
        filename: CSV filename.  Defaults to ``paper_trades_YYYYMMDD.csv``.
    """

    _FIELDNAMES = [
        "timestamp", "ticker", "mode",
        "long_score", "short_score", "confidence_label", "signal_type",
        "suggested_entry", "stop_loss", "target1", "target2", "reward_risk",
        "atr", "rsi14", "vwap", "rel_volume",
        "outcome_pnl", "outcome_note",
    ]

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        filename: Optional[str] = None,
    ) -> None:
        self._log_dir = log_dir or _DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            today = datetime.utcnow().strftime("%Y%m%d")
            filename = f"paper_trades_{today}.csv"
        self._filepath = self._log_dir / filename

        self._ensure_header()
        logger.info("PaperTradeLogger writing to %s", self._filepath)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(self, event: PaperTradeEvent) -> None:
        """Append a single event row to the CSV."""
        try:
            with open(self._filepath, "a", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=self._FIELDNAMES)
                row = {k: getattr(event, k, None) for k in self._FIELDNAMES}
                # Convert None to empty string so CSV stays clean
                row = {k: ("" if v is None else v) for k, v in row.items()}
                writer.writerow(row)
            logger.debug("Logged paper-trade event: %s %s", event.ticker, event.signal_type)
        except Exception as exc:
            logger.error("PaperTradeLogger.log() failed: %s", exc)

    def log_signal(
        self,
        score,          # ScoreResult
        snap,           # IndicatorSnapshot
        guidance,       # GuidanceResult
        signal_type: str = "",
        mode: str = "live",
    ) -> None:
        """Convenience wrapper that builds a PaperTradeEvent from engine outputs."""
        event = PaperTradeEvent(
            ticker=snap.symbol,
            mode=mode,
            long_score=round(score.long_score, 1),
            short_score=round(score.short_score, 1),
            confidence_label=score.confidence_label,
            signal_type=signal_type or score.confidence_label,
            suggested_entry=guidance.suggested_entry,
            stop_loss=guidance.stop_loss,
            target1=guidance.target1,
            target2=guidance.target2,
            reward_risk=guidance.reward_risk,
            atr=snap.atr14,
            rsi14=snap.rsi14,
            vwap=snap.vwap,
            rel_volume=round(snap.rel_volume, 2) if snap.rel_volume else None,
        )
        self.log(event)

    def log_outcome(
        self,
        ticker: str,
        entry_timestamp: str,
        pnl: float,
        note: str = "",
    ) -> None:
        """Update the outcome of an existing event (backtest mode).

        Reads the CSV, finds the row with matching ticker + timestamp,
        updates outcome fields, and rewrites the file.
        """
        try:
            rows = self._read_all()
            updated = False
            for row in rows:
                if row["ticker"] == ticker and row["timestamp"] == entry_timestamp:
                    row["outcome_pnl"]  = str(round(pnl, 4))
                    row["outcome_note"] = note
                    updated = True
                    break
            if updated:
                self._write_all(rows)
                logger.info("Outcome recorded for %s @ %s: PnL=%.4f", ticker, entry_timestamp, pnl)
            else:
                logger.warning("No matching row found for %s @ %s", ticker, entry_timestamp)
        except Exception as exc:
            logger.error("log_outcome failed: %s", exc)

    def read_log(self) -> list:
        """Return all logged events as a list of dicts."""
        return self._read_all()

    @property
    def filepath(self) -> Path:
        return self._filepath

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_header(self) -> None:
        if not self._filepath.exists() or self._filepath.stat().st_size == 0:
            with open(self._filepath, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=self._FIELDNAMES)
                writer.writeheader()

    def _read_all(self) -> list:
        rows = []
        try:
            with open(self._filepath, "r", newline="") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
        except Exception as exc:
            logger.error("_read_all failed: %s", exc)
        return rows

    def _write_all(self, rows: list) -> None:
        with open(self._filepath, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self._FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
