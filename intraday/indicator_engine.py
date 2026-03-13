"""
Intraday Indicator Engine
==========================
Computes all real-time technical indicators from OHLCV intraday bars
plus live quote data.  Results are returned as a typed ``IndicatorSnapshot``
dataclass so the scoring layer never touches raw DataFrames.

Indicators
----------
- Intraday VWAP (daily reset at 09:30 ET)
- SMA 20, 50, 200 (on 1-min closes; SMA200 requires daily bars fallback)
- SMA50 slope (over configurable N bars)
- RSI(14)
- MACD(12, 26, 9)
- ATR(14)
- Relative volume (current bar vs. rolling average of bar volume)
- Candle body / wick analysis (latest bar)
- Support and resistance levels (pivot-based)
- Breakout and breakdown detection

DISCLAIMER: For educational decision support only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("intraday.indicator_engine")

# Market open (Eastern Time) — used for VWAP daily reset
_MARKET_OPEN_ET = dtime(9, 30)


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class SRLevel:
    """A support or resistance price level."""
    price: float
    label: str       # "support" | "resistance"
    strength: int    # 1–3 (number of touches / confluence)


@dataclass
class IndicatorSnapshot:
    """All computed indicators for a single refresh cycle."""

    # ── Prices ──────────────────────────────────────────────
    symbol: str
    latest_close: float
    latest_open: float
    latest_high: float
    latest_low: float
    latest_volume: Optional[int] = None

    # ── VWAP ────────────────────────────────────────────────
    vwap: Optional[float] = None

    # ── Moving averages (on 1-min data) ─────────────────────
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    sma200: Optional[float] = None   # uses daily bars if intraday too short
    sma50_slope: Optional[float] = None   # signed slope over N bars

    # ── Momentum ─────────────────────────────────────────────
    rsi14: Optional[float] = None
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    macd_hist_prev: Optional[float] = None  # previous bar histogram

    # ── Volatility ───────────────────────────────────────────
    atr14: Optional[float] = None

    # ── Volume ───────────────────────────────────────────────
    rel_volume: Optional[float] = None    # current-bar vol / rolling avg

    # ── Candle structure (latest bar) ────────────────────────
    body_size: Optional[float] = None     # |close - open|
    body_pct: Optional[float] = None      # body / (high - low)
    upper_wick_pct: Optional[float] = None  # upper wick / (high - low)
    lower_wick_pct: Optional[float] = None  # lower wick / (high - low)
    candle_bullish: bool = False           # close >= open

    # ── Support / Resistance ─────────────────────────────────
    sr_levels: List[SRLevel] = field(default_factory=list)
    nearest_resistance: Optional[float] = None
    nearest_support: Optional[float] = None
    breakout: bool = False      # price convincingly above resistance
    breakdown: bool = False     # price convincingly below support

    # ── Bid-ask (from live quote) ─────────────────────────────
    spread: Optional[float] = None
    spread_pct: Optional[float] = None
    bid_ask_imbalance: Optional[float] = None  # bid/(bid+ask) size fraction


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class IndicatorEngine:
    """Compute all intraday indicators from OHLCV bars + live quote.

    Args:
        sr_lookback:     Number of recent 1-min bars to scan for S/R pivots.
        sr_min_touches:  Minimum pivot touching the same zone to record a level.
        sr_zone_pct:     Price range (fraction) to cluster pivots into one zone.
        sma50_slope_bars: Number of bars over which the SMA50 slope is measured.
        sr_proximity_pct: Fraction of price defining "near" a level.
        breakout_confirm_pct: Fraction above/below S/R to confirm breakout.
    """

    def __init__(
        self,
        sr_lookback: int = 100,
        sr_min_touches: int = 2,
        sr_zone_pct: float = 0.003,
        sma50_slope_bars: int = 5,
        sr_proximity_pct: float = 0.003,
        breakout_confirm_pct: float = 0.002,
    ) -> None:
        self._sr_lookback = sr_lookback
        self._sr_min_touches = sr_min_touches
        self._sr_zone_pct = sr_zone_pct
        self._sma50_slope_bars = sma50_slope_bars
        self._sr_proximity_pct = sr_proximity_pct
        self._breakout_confirm_pct = breakout_confirm_pct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        intraday_df: pd.DataFrame,
        quote,                         # QuoteData
        daily_df: Optional[pd.DataFrame] = None,
    ) -> IndicatorSnapshot:
        """Compute all indicators and return an ``IndicatorSnapshot``.

        Args:
            intraday_df: 1-min OHLCV DataFrame with DatetimeIndex.
            quote:       Latest ``QuoteData`` instance.
            daily_df:    Optional daily OHLCV DataFrame (for SMA200).
        """
        if intraday_df is None or intraday_df.empty:
            logger.warning("compute() called with empty intraday_df for %s", quote.symbol)
            price = quote.last or quote.mid or 0.0
            return IndicatorSnapshot(
                symbol=quote.symbol,
                latest_close=price,
                latest_open=price,
                latest_high=price,
                latest_low=price,
            )

        df = intraday_df.copy().sort_index()

        latest = df.iloc[-1]
        snap = IndicatorSnapshot(
            symbol=quote.symbol,
            latest_close=float(latest["Close"]),
            latest_open=float(latest["Open"]),
            latest_high=float(latest["High"]),
            latest_low=float(latest["Low"]),
            latest_volume=int(latest["Volume"]) if pd.notna(latest.get("Volume")) else None,
        )

        # ── Use live price as latest_close where available ────────────────
        if quote.last and quote.last > 0:
            snap.latest_close = quote.last

        close = df["Close"]

        self._compute_vwap(df, snap)
        self._compute_smas(df, close, daily_df, snap)
        self._compute_sma50_slope(df, snap)
        self._compute_rsi(close, snap)
        self._compute_macd(close, snap)
        self._compute_atr(df, snap)
        self._compute_rel_volume(df, snap)
        self._compute_candle_structure(latest, snap)
        self._compute_sr_levels(df, snap)
        self._compute_breakout(snap)

        # Quote-derived fields
        snap.spread = quote.spread
        snap.spread_pct = quote.spread_pct
        snap.bid_ask_imbalance = quote.bid_ask_imbalance

        return snap

    # ------------------------------------------------------------------
    # VWAP
    # ------------------------------------------------------------------

    def _compute_vwap(self, df: pd.DataFrame, snap: IndicatorSnapshot) -> None:
        """Intraday VWAP — resets at market open each day."""
        try:
            # Filter to today's bars (date of last bar)
            last_date = df.index[-1].date()
            today = df[df.index.date == last_date].copy()
            if today.empty or "Volume" not in today.columns:
                return
            tp = (today["High"] + today["Low"] + today["Close"]) / 3.0
            cum_tp_vol = (tp * today["Volume"]).cumsum()
            cum_vol = today["Volume"].cumsum()
            vwap_series = cum_tp_vol / cum_vol.replace(0, np.nan)
            v = vwap_series.iloc[-1]
            snap.vwap = float(v) if pd.notna(v) else None
        except Exception as exc:
            logger.debug("VWAP error: %s", exc)

    # ------------------------------------------------------------------
    # Moving averages
    # ------------------------------------------------------------------

    def _compute_smas(
        self,
        df: pd.DataFrame,
        close: pd.Series,
        daily_df: Optional[pd.DataFrame],
        snap: IndicatorSnapshot,
    ) -> None:
        """Compute SMA20, SMA50, SMA200."""
        def _sma(series: pd.Series, period: int) -> Optional[float]:
            if len(series) < period:
                return None
            val = series.rolling(period).mean().iloc[-1]
            return float(val) if pd.notna(val) else None

        snap.sma20 = _sma(close, 20)
        snap.sma50 = _sma(close, 50)

        # SMA200: prefer intraday bars, fall back to daily
        if len(close) >= 200:
            snap.sma200 = _sma(close, 200)
        elif daily_df is not None and not daily_df.empty:
            daily_close = daily_df["Close"]
            snap.sma200 = _sma(daily_close, 200)

    # ------------------------------------------------------------------
    # SMA50 slope
    # ------------------------------------------------------------------

    def _compute_sma50_slope(self, df: pd.DataFrame, snap: IndicatorSnapshot) -> None:
        """Signed slope of SMA50 over _sma50_slope_bars bars."""
        try:
            close = df["Close"]
            if len(close) < 50 + self._sma50_slope_bars:
                return
            sma50 = close.rolling(50).mean().dropna()
            if len(sma50) < self._sma50_slope_bars:
                return
            recent = sma50.iloc[-self._sma50_slope_bars:]
            # Simple linear regression slope
            x = np.arange(len(recent), dtype=float)
            slope = float(np.polyfit(x, recent.values, 1)[0])
            snap.sma50_slope = slope
        except Exception as exc:
            logger.debug("SMA50 slope error: %s", exc)

    # ------------------------------------------------------------------
    # RSI
    # ------------------------------------------------------------------

    def _compute_rsi(self, close: pd.Series, snap: IndicatorSnapshot) -> None:
        try:
            if len(close) < 15:
                return
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            val = rsi.iloc[-1]
            snap.rsi14 = float(val) if pd.notna(val) else None
        except Exception as exc:
            logger.debug("RSI error: %s", exc)

    # ------------------------------------------------------------------
    # MACD
    # ------------------------------------------------------------------

    def _compute_macd(self, close: pd.Series, snap: IndicatorSnapshot) -> None:
        try:
            if len(close) < 27:
                return
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal = macd_line.ewm(span=9, adjust=False).mean()
            hist = macd_line - signal

            snap.macd_line = float(macd_line.iloc[-1]) if pd.notna(macd_line.iloc[-1]) else None
            snap.macd_signal = float(signal.iloc[-1]) if pd.notna(signal.iloc[-1]) else None
            snap.macd_hist = float(hist.iloc[-1]) if pd.notna(hist.iloc[-1]) else None
            if len(hist) > 1:
                prev = hist.iloc[-2]
                snap.macd_hist_prev = float(prev) if pd.notna(prev) else None
        except Exception as exc:
            logger.debug("MACD error: %s", exc)

    # ------------------------------------------------------------------
    # ATR
    # ------------------------------------------------------------------

    def _compute_atr(self, df: pd.DataFrame, snap: IndicatorSnapshot) -> None:
        try:
            if len(df) < 15:
                return
            high = df["High"]
            low = df["Low"]
            prev_close = df["Close"].shift(1)
            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr = tr.rolling(14).mean().iloc[-1]
            snap.atr14 = float(atr) if pd.notna(atr) else None
        except Exception as exc:
            logger.debug("ATR error: %s", exc)

    # ------------------------------------------------------------------
    # Relative volume
    # ------------------------------------------------------------------

    def _compute_rel_volume(self, df: pd.DataFrame, snap: IndicatorSnapshot) -> None:
        """Relative volume: current bar's volume vs. rolling 20-bar average."""
        try:
            if "Volume" not in df.columns or len(df) < 5:
                return
            vol = df["Volume"].dropna()
            if len(vol) < 5:
                return
            avg = vol.iloc[:-1].rolling(min(20, len(vol) - 1)).mean().iloc[-1]
            if avg and avg > 0:
                snap.rel_volume = float(vol.iloc[-1]) / float(avg)
        except Exception as exc:
            logger.debug("RelVol error: %s", exc)

    # ------------------------------------------------------------------
    # Candle structure
    # ------------------------------------------------------------------

    def _compute_candle_structure(self, latest: pd.Series, snap: IndicatorSnapshot) -> None:
        try:
            hi = float(latest["High"])
            lo = float(latest["Low"])
            op = float(latest["Open"])
            cl = float(latest["Close"])
            candle_range = hi - lo
            if candle_range == 0:
                return
            body = abs(cl - op)
            upper_wick = hi - max(cl, op)
            lower_wick = min(cl, op) - lo

            snap.body_size = body
            snap.body_pct = body / candle_range
            snap.upper_wick_pct = upper_wick / candle_range
            snap.lower_wick_pct = lower_wick / candle_range
            snap.candle_bullish = cl >= op
        except Exception as exc:
            logger.debug("Candle structure error: %s", exc)

    # ------------------------------------------------------------------
    # Support / Resistance
    # ------------------------------------------------------------------

    def _compute_sr_levels(self, df: pd.DataFrame, snap: IndicatorSnapshot) -> None:
        """Identify S/R levels from pivot highs/lows in recent bars."""
        try:
            lookback_df = df.tail(self._sr_lookback)
            pivots = self._find_pivots(lookback_df)
            levels = self._cluster_pivots(pivots, snap.latest_close)
            snap.sr_levels = levels

            price = snap.latest_close
            resistances = [lvl.price for lvl in levels if lvl.label == "resistance" and lvl.price > price]
            supports = [lvl.price for lvl in levels if lvl.label == "support" and lvl.price < price]

            snap.nearest_resistance = min(resistances, default=None)
            snap.nearest_support = max(supports, default=None)
        except Exception as exc:
            logger.debug("S/R error: %s", exc)

    def _find_pivots(self, df: pd.DataFrame, n: int = 3) -> List[Tuple[float, str]]:
        """Return list of (price, 'high'|'low') pivot points."""
        pivots: List[Tuple[float, str]] = []
        highs = df["High"].values
        lows = df["Low"].values
        for i in range(n, len(df) - n):
            if all(highs[i] > highs[i - j] for j in range(1, n + 1)) and \
               all(highs[i] > highs[i + j] for j in range(1, n + 1)):
                pivots.append((highs[i], "high"))
            if all(lows[i] < lows[i - j] for j in range(1, n + 1)) and \
               all(lows[i] < lows[i + j] for j in range(1, n + 1)):
                pivots.append((lows[i], "low"))
        return pivots

    def _cluster_pivots(
        self, pivots: List[Tuple[float, str]], current_price: float
    ) -> List[SRLevel]:
        """Group nearby pivots into S/R zones."""
        if not pivots:
            return []

        # Sort by price descending
        sorted_pivots = sorted(pivots, key=lambda x: x[0], reverse=True)

        clusters: List[List[Tuple[float, str]]] = []
        for price, kind in sorted_pivots:
            placed = False
            for cluster in clusters:
                ref = cluster[0][0]
                if abs(price - ref) / max(ref, 1e-9) <= self._sr_zone_pct:
                    cluster.append((price, kind))
                    placed = True
                    break
            if not placed:
                clusters.append([(price, kind)])

        levels: List[SRLevel] = []
        for cluster in clusters:
            if len(cluster) < self._sr_min_touches:
                # Still include single pivots if they are very recent but label strength=1
                if len(cluster) < 1:
                    continue
            avg_price = float(np.mean([p for p, _ in cluster]))
            label = "resistance" if avg_price > current_price else "support"
            levels.append(SRLevel(
                price=round(avg_price, 4),
                label=label,
                strength=min(len(cluster), 3),
            ))

        return sorted(levels, key=lambda l: l.price)

    # ------------------------------------------------------------------
    # Breakout / breakdown
    # ------------------------------------------------------------------

    def _compute_breakout(self, snap: IndicatorSnapshot) -> None:
        try:
            price = snap.latest_close
            if snap.nearest_resistance:
                threshold = snap.nearest_resistance * (1 + self._breakout_confirm_pct)
                snap.breakout = price > threshold
            if snap.nearest_support:
                threshold = snap.nearest_support * (1 - self._breakout_confirm_pct)
                snap.breakdown = price < threshold
        except Exception as exc:
            logger.debug("Breakout compute error: %s", exc)
