"""
Auto Trader — Automated position management for the Live Dashboard.

Supports two modes:
  - Learning mode (default ON): observes signals and runs PriceAnalyzer; no orders sent.
  - Live trading mode: executes LONG/SHORT orders through IBKRConnector when confidence
    thresholds are met.

All entry/exit methods are synchronous (no asyncio.create_task) so they integrate
cleanly with Streamlit's single-thread model.

Ported and adapted from trader.ai/src/atr_analysis/live_dashboard/auto_trader.py.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

from ib_insync import MarketOrder

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Immutable record of one trade entry or exit."""
    trade_id: str
    ticker: str
    direction: str           # 'LONG' | 'SHORT'
    quantity: int
    entry_price: float
    exit_price: Optional[float]
    stop_loss: float
    take_profit: float
    entry_time: str
    exit_time: Optional[str]
    status: str              # 'OPEN' | 'CLOSED' | 'CANCELLED'
    pnl: float
    pnl_percent: float
    exit_reason: Optional[str]   # 'STOP_LOSS' | 'TAKE_PROFIT' | 'SIGNAL_CHANGE' | 'MANUAL'

    def to_dict(self) -> Dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# AutoTrader
# ─────────────────────────────────────────────────────────────────────────────

class AutoTrader:
    """
    Automated LONG/SHORT trading based on mean-reversion signals.

    Parameters
    ----------
    ibkr : IBKRConnector
        Connected connector instance.
    allocation_per_ticker : float
        Dollar amount committed per position (default $25 000).
    stop_loss_pct : float
        Stop-loss as percentage (2.5 → 2.5%).
    take_profit_pct : float
        Take-profit as percentage (1.5 → 1.5%).
    trade_history_file : Path
        JSON file where closed trades are persisted.
    min_confidence : float
        Minimum PriceAnalyzer confidence to enter a trade (0-100).
    """

    def __init__(
        self,
        ibkr,
        allocation_per_ticker: float = 25_000.0,
        stop_loss_pct: float = 2.5,
        take_profit_pct: float = 1.5,
        trade_history_file: Path = Path("data/trade_history.json"),
        min_confidence: float = 60.0,
        analyzer=None,
    ):
        self.ibkr = ibkr
        self.allocation = allocation_per_ticker
        self.sl_pct = stop_loss_pct / 100.0
        self.tp_pct = take_profit_pct / 100.0
        self.history_file = Path(trade_history_file)
        self.min_confidence = min_confidence
        self.analyzer = analyzer       # Optional PriceAnalyzer instance

        # Runtime state
        self._lock = Lock()
        self.auto_trading_enabled: bool = False
        self.learning_mode: bool = True
        self.allowed_tickers: List[str] = []
        self.active_positions: Dict[str, TradeRecord] = {}
        self.trade_history: List[TradeRecord] = []
        self.analysis_complete: Dict[str, bool] = {}

        self._load_history()
        logger.info(
            f"AutoTrader ready — ${allocation_per_ticker:.0f}/ticker, "
            f"SL={stop_loss_pct}%, TP={take_profit_pct}%"
        )

    # ── Control ───────────────────────────────────────────────────────────────

    def enable_trading(self, tickers: List[str]):
        with self._lock:
            self.auto_trading_enabled = True
            self.allowed_tickers = list(tickers)
        logger.info(f"Auto trading ENABLED for: {', '.join(tickers)}")

    def disable_trading(self):
        with self._lock:
            self.auto_trading_enabled = False
        logger.info("Auto trading DISABLED")

    def enable_learning_mode(self):
        with self._lock:
            self.learning_mode = True
        logger.info("Learning mode ENABLED — observing only")

    def disable_learning_mode(self):
        with self._lock:
            self.learning_mode = False
        logger.info("Learning mode DISABLED — live trading active")

    # ── Signal processing ─────────────────────────────────────────────────────

    def process_signal(
        self,
        ticker: str,
        signal: str,
        current_price: float,
        signal_strength: int = 0,
    ):
        """
        Called once per tick. Decides whether to enter/exit/manage a position.

        Args:
            ticker : Ticker symbol.
            signal : STRONG_LONG | LONG | NEUTRAL | SHORT | STRONG_SHORT
            current_price : Latest trade price.
            signal_strength : Raw strength value (negative=long, positive=short).
        """
        if not self.auto_trading_enabled:
            return
        if ticker not in self.allowed_tickers:
            return

        # Learning mode — only observe, never trade
        if self.learning_mode:
            logger.debug(f"[{ticker}] Learning mode — signal={signal} price={current_price:.2f}")
            return

        # Intelligent mode: use PriceAnalyzer if available
        if self.analyzer:
            analysis = self.analyzer.get_analysis(ticker)
            if not analysis:
                # Trigger analysis (synchronous — may be slow first call)
                logger.info(f"[{ticker}] Triggering on-demand analysis…")
                analysis = self.analyzer.analyze_ticker(ticker, current_price)

            if analysis and not analysis.should_trade:
                logger.debug(f"[{ticker}] Analyzer says WAIT: {analysis.reason}")
                self._check_manage_position(ticker, current_price, signal)
                return

            if analysis and analysis.should_trade:
                self._execute_from_analysis(ticker, current_price, signal, analysis)
                return

        # Simple signal mode (fallback)
        self._execute_from_signal(ticker, current_price, signal)

    def _execute_from_analysis(self, ticker, price, signal, analysis):
        rec = analysis.recommendation
        with self._lock:
            if rec == "LONG" and not self._has_position(ticker):
                self._enter_long(ticker, price)
            elif rec == "SHORT" and not self._has_position(ticker):
                self._enter_short(ticker, price)
            elif self._has_position(ticker):
                self._manage_position(ticker, price, signal)

    def _execute_from_signal(self, ticker, price, signal):
        with self._lock:
            if signal in ("STRONG_LONG", "LONG") and not self._has_position(ticker):
                self._enter_long(ticker, price)
            elif signal in ("STRONG_SHORT", "SHORT") and not self._has_position(ticker):
                self._enter_short(ticker, price)
            elif self._has_position(ticker):
                self._manage_position(ticker, price, signal)

    def _check_manage_position(self, ticker, price, signal):
        with self._lock:
            if self._has_position(ticker):
                self._manage_position(ticker, price, signal)

    # ── Entry / Exit ──────────────────────────────────────────────────────────

    def _enter_long(self, ticker: str, entry_price: float):
        qty = int(self.allocation / entry_price)
        if qty <= 0:
            return
        sl = round(entry_price * (1 - self.sl_pct), 2)
        tp = round(entry_price * (1 + self.tp_pct), 2)

        if self.ibkr and self.ibkr.is_connected():
            try:
                order = MarketOrder("BUY", qty)
                self.ibkr.place_order(ticker, order)
            except Exception as exc:
                logger.error(f"[{ticker}] LONG order failed: {exc}")
                return

        record = TradeRecord(
            trade_id=f"{ticker}_LONG_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            ticker=ticker, direction="LONG", quantity=qty,
            entry_price=entry_price, exit_price=None,
            stop_loss=sl, take_profit=tp,
            entry_time=datetime.now().isoformat(), exit_time=None,
            status="OPEN", pnl=0.0, pnl_percent=0.0, exit_reason=None,
        )
        self.active_positions[ticker] = record
        logger.info(
            f"[{ticker}] LONG ENTERED {qty} @ ${entry_price:.2f} "
            f"SL=${sl:.2f} TP=${tp:.2f}"
        )

    def _enter_short(self, ticker: str, entry_price: float):
        qty = int(self.allocation / entry_price)
        if qty <= 0:
            return
        sl = round(entry_price * (1 + self.sl_pct), 2)
        tp = round(entry_price * (1 - self.tp_pct), 2)

        if self.ibkr and self.ibkr.is_connected():
            try:
                order = MarketOrder("SELL", qty)
                self.ibkr.place_order(ticker, order)
            except Exception as exc:
                logger.error(f"[{ticker}] SHORT order failed: {exc}")
                return

        record = TradeRecord(
            trade_id=f"{ticker}_SHORT_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            ticker=ticker, direction="SHORT", quantity=qty,
            entry_price=entry_price, exit_price=None,
            stop_loss=sl, take_profit=tp,
            entry_time=datetime.now().isoformat(), exit_time=None,
            status="OPEN", pnl=0.0, pnl_percent=0.0, exit_reason=None,
        )
        self.active_positions[ticker] = record
        logger.info(
            f"[{ticker}] SHORT ENTERED {qty} @ ${entry_price:.2f} "
            f"SL=${sl:.2f} TP=${tp:.2f}"
        )

    def _manage_position(self, ticker: str, price: float, signal: str):
        """Check stop-loss, take-profit, or signal reversal."""
        record = self.active_positions.get(ticker)
        if not record:
            return

        reason = None
        if record.direction == "LONG":
            if price <= record.stop_loss:
                reason = "STOP_LOSS"
            elif price >= record.take_profit:
                reason = "TAKE_PROFIT"
            elif signal in ("STRONG_SHORT", "SHORT"):
                reason = "SIGNAL_CHANGE"
        else:  # SHORT
            if price >= record.stop_loss:
                reason = "STOP_LOSS"
            elif price <= record.take_profit:
                reason = "TAKE_PROFIT"
            elif signal in ("STRONG_LONG", "LONG"):
                reason = "SIGNAL_CHANGE"

        if reason:
            self._close_position(ticker, price, reason)

    def _close_position(self, ticker: str, exit_price: float, reason: str):
        record = self.active_positions.get(ticker)
        if not record:
            return

        if self.ibkr and self.ibkr.is_connected():
            try:
                action = "SELL" if record.direction == "LONG" else "BUY"
                order = MarketOrder(action, record.quantity)
                self.ibkr.place_order(ticker, order)
            except Exception as exc:
                logger.error(f"[{ticker}] Close order failed: {exc}")

        if record.direction == "LONG":
            pnl = (exit_price - record.entry_price) * record.quantity
            pnl_pct = (exit_price - record.entry_price) / record.entry_price * 100
        else:
            pnl = (record.entry_price - exit_price) * record.quantity
            pnl_pct = (record.entry_price - exit_price) / record.entry_price * 100

        record.exit_price   = exit_price
        record.exit_time    = datetime.now().isoformat()
        record.status       = "CLOSED"
        record.pnl          = round(pnl, 2)
        record.pnl_percent  = round(pnl_pct, 2)
        record.exit_reason  = reason

        self.trade_history.append(record)
        del self.active_positions[ticker]
        self._save_history()

        logger.info(
            f"[{ticker}] {record.direction} CLOSED @ ${exit_price:.2f} "
            f"P&L ${pnl:.2f} ({pnl_pct:+.2f}%) [{reason}]"
        )

    # ── Manual close all ──────────────────────────────────────────────────────

    def close_all_positions(self, current_prices: Optional[Dict[str, float]] = None):
        """Manually close every open position at market."""
        with self._lock:
            tickers = list(self.active_positions.keys())
        for ticker in tickers:
            price = (current_prices or {}).get(ticker, 0.0)
            if price <= 0:
                # Estimate from open positions record
                price = self.active_positions[ticker].entry_price
            self._close_position(ticker, price, "MANUAL")

    # ── Queries ───────────────────────────────────────────────────────────────

    def _has_position(self, ticker: str) -> bool:
        return ticker in self.active_positions

    def get_active_positions(self) -> List[Dict]:
        with self._lock:
            return [r.to_dict() for r in self.active_positions.values()]

    def get_trade_history(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            recent = self.trade_history[-limit:]
            return [r.to_dict() for r in reversed(recent)]

    def get_trading_stats(self) -> Dict:
        with self._lock:
            closed = [r for r in self.trade_history if r.status == "CLOSED"]
            if not closed:
                return {
                    "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
                    "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                    "best_trade": 0.0, "worst_trade": 0.0,
                    "active_positions": len(self.active_positions),
                }
            wins   = [r for r in closed if r.pnl > 0]
            losses = [r for r in closed if r.pnl <= 0]
            pnls   = [r.pnl for r in closed]
            return {
                "total_trades":    len(closed),
                "winning_trades":  len(wins),
                "losing_trades":   len(losses),
                "win_rate":        round(len(wins) / len(closed) * 100, 1),
                "total_pnl":       round(sum(pnls), 2),
                "avg_pnl":         round(sum(pnls) / len(pnls), 2),
                "best_trade":      round(max(pnls), 2),
                "worst_trade":     round(min(pnls), 2),
                "active_positions": len(self.active_positions),
            }

    def get_learning_status(self) -> Dict:
        analyzed = sum(1 for v in self.analysis_complete.values() if v)
        total    = len(self.analysis_complete)
        return {
            "learning_mode":     self.learning_mode,
            "analysis_enabled":  self.analyzer is not None,
            "tickers_analyzed":  analyzed,
            "total_tickers":     total,
            "analysis_progress": f"{analyzed}/{total}",
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_history(self):
        if self.history_file.exists():
            try:
                with open(self.history_file) as f:
                    data = json.load(f)
                self.trade_history = [TradeRecord(**d) for d in data]
                logger.info(f"Loaded {len(self.trade_history)} historical trades")
            except Exception as exc:
                logger.error(f"Could not load trade history: {exc}")

    def _save_history(self):
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, "w") as f:
                json.dump([r.to_dict() for r in self.trade_history], f, indent=2)
        except Exception as exc:
            logger.error(f"Could not save trade history: {exc}")
