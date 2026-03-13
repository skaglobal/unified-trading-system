"""
Intraday Backtest Mode
=======================
Replay historical 1-minute bars through the scoring engine to validate
the signal logic.  Results are written to the paper-trade logger so you
can review them alongside live sessions.

Usage
-----
    from intraday.backtest_mode import IntradayBacktester

    # Requires an IBKR connection for data fetch
    bt = IntradayBacktester(ibkr_connector=connector)
    results = bt.run("AAPL", date(2025, 3, 10))
    print(results.summary())

DISCLAIMER: Past-signal performance does not guarantee future results.
            Educational tool only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional

import pandas as pd

from intraday.indicator_engine import IndicatorEngine, IndicatorSnapshot
from intraday.scoring_engine import ScoringEngine, ScoreResult
from intraday.guidance_engine import GuidanceEngine, GuidanceResult
from intraday.alert_engine import AlertEngine, Alert
from intraday.paper_trade_logger import PaperTradeLogger, PaperTradeEvent
from intraday.market_data_provider import MarketDataProvider, QuoteData

logger = logging.getLogger("intraday.backtest_mode")


# ---------------------------------------------------------------------------
# Backtest result
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """A simulated trade recorded during backtest replay."""
    timestamp: str
    direction: str            # "long" | "short"
    entry: float
    stop: float
    target1: float
    target2: float
    long_score: float
    short_score: float
    confidence_label: str
    exit_price: Optional[float] = None
    exit_reason: str = ""     # "target1" | "target2" | "stop" | "eod"
    pnl: Optional[float] = None
    hit_target1: bool = False
    hit_target2: bool = False
    stopped_out: bool = False


@dataclass
class BacktestResult:
    """Summary statistics for a completed backtest run."""
    symbol: str
    run_date: str
    total_signals: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    win_rate: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"=== Backtest: {self.symbol} on {self.run_date} ===",
            f"  Total signals : {self.total_signals}",
            f"  Total trades  : {self.total_trades}",
            f"  Win rate      : {self.win_rate:.1%}",
            f"  Gross PnL     : ${self.gross_pnl:.2f} per share",
            f"  Avg winner    : ${self.avg_winner:.2f}",
            f"  Avg loser     : ${self.avg_loser:.2f}",
            "=" * 40,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class IntradayBacktester:
    """Replay historical 1-min bars through the full signal pipeline.

    Args:
        provider:        A ``MarketDataProvider`` instance for fetching bars.
        min_signal_score: Minimum dominant-side score to open a simulated trade.
        warmup_bars:      Number of bars consumed silently before scoring starts
                          (allows indicators to warm up).
        log_to_csv:       If True, events are written via PaperTradeLogger.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        min_signal_score: float = 60.0,
        warmup_bars: int = 50,
        log_to_csv: bool = True,
    ) -> None:
        self._provider = provider
        self._min_signal_score = min_signal_score
        self._warmup_bars = warmup_bars

        self._indicator_engine = IndicatorEngine()
        self._scoring_engine   = ScoringEngine()
        self._guidance_engine  = GuidanceEngine()
        self._alert_engine     = AlertEngine()
        self._pt_logger        = PaperTradeLogger() if log_to_csv else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        symbol: str,
        target_date: Optional[date] = None,
        duration: str = "1 D",
    ) -> BacktestResult:
        """Run the backtest for *symbol* on *target_date*.

        Args:
            symbol:      Ticker to simulate.
            target_date: Date to replay.  Defaults to yesterday.
            duration:    IBKR duration string for historical data.
        """
        if target_date is None:
            target_date = date.today() - timedelta(days=1)

        logger.info("Starting backtest: %s on %s", symbol, target_date)

        # Fetch 1-min bars
        df_intraday = self._provider.get_intraday_candles(
            symbol=symbol,
            bar_size="1 min",
            duration=duration,
            use_rth=True,
        )
        if df_intraday is None or df_intraday.empty:
            logger.error("No intraday data for %s on %s", symbol, target_date)
            return BacktestResult(symbol=symbol, run_date=str(target_date))

        # Optionally filter to target_date
        try:
            df_intraday.index = pd.to_datetime(df_intraday.index)
            mask = df_intraday.index.date == target_date
            daily_bars = df_intraday[mask]
            if daily_bars.empty:
                logger.warning("No bars for %s on %s, using all available data", symbol, target_date)
                daily_bars = df_intraday
        except Exception:
            daily_bars = df_intraday

        # Daily bars for SMA200
        df_daily = self._provider.get_daily_candles(symbol=symbol)

        return self._replay(symbol, str(target_date), daily_bars, df_daily)

    # ------------------------------------------------------------------
    # Replay loop
    # ------------------------------------------------------------------

    def _replay(
        self,
        symbol: str,
        run_date: str,
        df: pd.DataFrame,
        df_daily: Optional[pd.DataFrame],
    ) -> BacktestResult:
        result = BacktestResult(symbol=symbol, run_date=run_date)

        open_trade: Optional[BacktestTrade] = None
        prev_score: Optional[ScoreResult] = None

        for i in range(self._warmup_bars, len(df)):
            # Give the engine the bars up to *and including* bar i
            window = df.iloc[: i + 1]
            bar = df.iloc[i]

            # Synthetic quote from OHLCV bar (no live bid/ask in backtest)
            quote = QuoteData(
                symbol=symbol,
                last=float(bar["Close"]),
                bid=float(bar["Close"]) - 0.01,
                ask=float(bar["Close"]) + 0.01,
                bid_size=int(bar.get("Volume", 100)),
                ask_size=int(bar.get("Volume", 100)),
                high=float(bar["High"]),
                low=float(bar["Low"]),
                open=float(bar["Open"]),
                volume=int(bar.get("Volume", 0)),
                source="backtest",
            )

            # Compute indicators
            snap: IndicatorSnapshot = self._indicator_engine.compute(
                intraday_df=window,
                quote=quote,
                daily_df=df_daily,
            )

            # Score
            score: ScoreResult = self._scoring_engine.score(snap)
            result.total_signals += 1

            # ── Manage open trade ──────────────────────────────────────────
            if open_trade is not None:
                close_price = float(bar["Close"])
                high_price  = float(bar["High"])
                low_price   = float(bar["Low"])

                if open_trade.direction == "long":
                    if low_price <= open_trade.stop:
                        self._close_trade(open_trade, open_trade.stop, "stop", result)
                        open_trade = None
                    elif high_price >= open_trade.target2:
                        self._close_trade(open_trade, open_trade.target2, "target2", result)
                        open_trade = None
                    elif high_price >= open_trade.target1:
                        open_trade.hit_target1 = True
                else:  # short
                    if high_price >= open_trade.stop:
                        self._close_trade(open_trade, open_trade.stop, "stop", result)
                        open_trade = None
                    elif low_price <= open_trade.target2:
                        self._close_trade(open_trade, open_trade.target2, "target2", result)
                        open_trade = None
                    elif low_price <= open_trade.target1:
                        open_trade.hit_target1 = True

            # ── Open new trade if no open trade and signal is strong ───────
            if open_trade is None and score.dominant_side != "none":
                dom_score = score.long_score if score.dominant_side == "long" else score.short_score
                if dom_score >= self._min_signal_score:
                    guidance: GuidanceResult = self._guidance_engine.compute_guidance(
                        score=score, snap=snap, prev_score=prev_score
                    )
                    if guidance.suggested_entry and guidance.stop_loss and guidance.target1 and guidance.target2:
                        ts = str(df.index[i])
                        open_trade = BacktestTrade(
                            timestamp=ts,
                            direction=score.dominant_side,
                            entry=guidance.suggested_entry,
                            stop=guidance.stop_loss,
                            target1=guidance.target1,
                            target2=guidance.target2,
                            long_score=score.long_score,
                            short_score=score.short_score,
                            confidence_label=score.confidence_label,
                        )
                        result.total_trades += 1

                        if self._pt_logger:
                            self._pt_logger.log_signal(score, snap, guidance, mode="backtest")

            prev_score = score

        # Close any trade still open at end-of-day
        if open_trade is not None and len(df) > 0:
            eod_price = float(df.iloc[-1]["Close"])
            self._close_trade(open_trade, eod_price, "eod", result)

        # ── Compute summary stats ──────────────────────────────────────────
        if result.trades:
            winners = [t for t in result.trades if (t.pnl or 0) > 0]
            losers  = [t for t in result.trades if (t.pnl or 0) <= 0]
            result.winning_trades = len(winners)
            result.losing_trades  = len(losers)
            result.gross_pnl      = sum(t.pnl or 0 for t in result.trades)
            result.win_rate       = len(winners) / len(result.trades)
            result.avg_winner     = sum(t.pnl or 0 for t in winners) / max(len(winners), 1)
            result.avg_loser      = sum(t.pnl or 0 for t in losers)  / max(len(losers),  1)

        logger.info(result.summary())
        return result

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _close_trade(
        self,
        trade: BacktestTrade,
        exit_price: float,
        reason: str,
        result: BacktestResult,
    ) -> None:
        trade.exit_price  = exit_price
        trade.exit_reason = reason

        if trade.direction == "long":
            trade.pnl = exit_price - trade.entry
        else:
            trade.pnl = trade.entry - exit_price

        if reason == "target1":
            trade.hit_target1 = True
        elif reason == "target2":
            trade.hit_target2 = True
        elif reason == "stop":
            trade.stopped_out = True

        result.trades.append(trade)

        if self._pt_logger:
            self._pt_logger.log_outcome(
                ticker=trade.direction,
                entry_timestamp=trade.timestamp,
                pnl=trade.pnl or 0.0,
                note=reason,
            )

        logger.debug(
            "Trade closed: %s %s entry=%.2f exit=%.2f pnl=%.2f reason=%s",
            trade.direction, result.symbol, trade.entry, exit_price, trade.pnl or 0, reason,
        )
