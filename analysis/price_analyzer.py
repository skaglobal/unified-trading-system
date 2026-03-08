"""
Price Analyzer — Support/Resistance and Pattern Analysis

Analyzes 20 days of 1-min IBKR historical bars to identify:
  - Significant support and resistance levels (swing-high/low clustering)
  - DIP_BOUNCE and PEAK_REVERSAL trading patterns with win rates
  - Confidence-based recommendation: LONG / SHORT / WAIT / AVOID

Ported from trader.ai/src/atr_analysis/live_dashboard/price_analyzer.py
with all async/await removed for Streamlit compatibility.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PriceLevel:
    """A significant price level (support or resistance)."""
    price: float
    level_type: str        # 'SUPPORT' | 'RESISTANCE'
    touches: int           # Times price approached the level
    bounces: int           # Times price bounced from the level
    breaks: int            # Times price broke through
    last_interaction: str  # ISO timestamp of last touch
    success_rate: float    # bounce / (bounce + breaks)
    strength: float        # 0-100 composite score

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class TradingPattern:
    """Identified recurring pattern at a price level."""
    price_level: float
    pattern_type: str          # 'DIP_BOUNCE' | 'PEAK_REVERSAL'
    occurrences: int
    avg_gain_pct: float
    avg_loss_pct: float
    win_rate: float            # 0-1
    avg_duration_minutes: int
    confidence: float          # 0-100

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class TickerAnalysis:
    """Complete analysis output for one ticker."""
    ticker: str
    analysis_date: str
    days_analyzed: int
    data_points: int

    # Price statistics
    avg_price: float
    price_volatility: float
    avg_daily_range_pct: float
    avg_volume: int

    # Key levels
    support_levels: List[PriceLevel]
    resistance_levels: List[PriceLevel]

    # Patterns
    patterns: List[TradingPattern]

    # Current position assessment
    current_price: float
    nearest_support: Optional[float]
    nearest_resistance: Optional[float]
    distance_to_support_pct: float
    distance_to_resistance_pct: float

    # Trading recommendation
    long_confidence: float   # 0-100
    short_confidence: float  # 0-100
    should_trade: bool
    recommendation: str      # 'LONG' | 'SHORT' | 'WAIT' | 'AVOID'
    reason: str

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Main analyser class
# ─────────────────────────────────────────────────────────────────────────────

class PriceAnalyzer:
    """
    Fetch IBKR historical data and produce support/resistance + pattern analysis.

    Usage (synchronous — designed for Streamlit / ib_insync IB instance):

        analyzer = PriceAnalyzer(ibkr_connector, lookback_days=20)
        analysis = analyzer.analyze_ticker("AAPL", current_price=172.50)
        print(analysis.recommendation, analysis.long_confidence)
    """

    def __init__(
        self,
        ibkr,                               # IBKRConnector (unified system)
        lookback_days: int = 20,
        min_confidence_threshold: float = 60.0,
        cache_dir: Path = Path("analysis_cache"),
    ):
        self.ibkr = ibkr
        self.lookback_days = lookback_days
        self.min_confidence_threshold = min_confidence_threshold
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # In-memory cache
        self._cache: Dict[str, TickerAnalysis] = {}

        logger.info(
            f"PriceAnalyzer ready — {lookback_days}d lookback, "
            f"{min_confidence_threshold}% min confidence"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze_ticker(self, ticker: str, current_price: float) -> "TickerAnalysis":
        """
        Full analysis pipeline — synchronous.
        Returns TickerAnalysis; falls back to empty analysis on any error.
        """
        logger.info(f"[{ticker}] Starting historical analysis…")
        try:
            df = self._fetch_historical_data(ticker)
            if df is None or df.empty or len(df) < 10:
                logger.warning(f"[{ticker}] Insufficient data")
                return self._empty_analysis(ticker, current_price)

            stats = self._calc_stats(df)
            supports, resistances = self._identify_levels(df, current_price)
            patterns = self._identify_patterns(df, supports, resistances)
            near_sup, near_res = self._find_nearest(current_price, supports, resistances)

            dist_sup = (
                (current_price - near_sup) / current_price * 100
                if near_sup else 999.0
            )
            dist_res = (
                (near_res - current_price) / current_price * 100
                if near_res else 999.0
            )

            long_conf, short_conf, should_trade, rec, reason = self._recommendation(
                current_price, near_sup, near_res, dist_sup, dist_res, patterns, stats
            )

            analysis = TickerAnalysis(
                ticker=ticker,
                analysis_date=datetime.now().isoformat(),
                days_analyzed=self.lookback_days,
                data_points=len(df),
                avg_price=stats["avg_price"],
                price_volatility=stats["volatility"],
                avg_daily_range_pct=stats["avg_range_pct"],
                avg_volume=int(stats["avg_volume"]),
                support_levels=supports,
                resistance_levels=resistances,
                patterns=patterns,
                current_price=current_price,
                nearest_support=near_sup,
                nearest_resistance=near_res,
                distance_to_support_pct=dist_sup,
                distance_to_resistance_pct=dist_res,
                long_confidence=long_conf,
                short_confidence=short_conf,
                should_trade=should_trade,
                recommendation=rec,
                reason=reason,
            )

            self._cache[ticker] = analysis
            self._save_cache(analysis)

            logger.info(
                f"[{ticker}] Analysis → {rec} "
                f"(L:{long_conf:.0f}% S:{short_conf:.0f}%)"
            )
            return analysis

        except Exception as exc:
            logger.error(f"[{ticker}] Analysis failed: {exc}", exc_info=True)
            return self._empty_analysis(ticker, current_price)

    def get_analysis(self, ticker: str) -> Optional["TickerAnalysis"]:
        """Return cached analysis for ticker, or None."""
        return self._cache.get(ticker)

    def get_cached_or_analyze(
        self, ticker: str, current_price: float, max_age_minutes: int = 30
    ) -> "TickerAnalysis":
        """
        Return cached analysis if recent enough; otherwise run fresh analysis.
        """
        cached = self._cache.get(ticker)
        if cached:
            try:
                age = (
                    datetime.now()
                    - datetime.fromisoformat(cached.analysis_date)
                ).total_seconds() / 60
                if age < max_age_minutes:
                    return cached
            except Exception:
                pass
        return self.analyze_ticker(ticker, current_price)

    # ── Data fetch ────────────────────────────────────────────────────────────

    def _fetch_historical_data(self, ticker: str) -> Optional[pd.DataFrame]:
        """Fetch 1-min bars for lookback_days using IBKRConnector."""
        try:
            df = self.ibkr.fetch_historical_data(
                symbol=ticker,
                duration=f"{self.lookback_days} D",
                bar_size="1 min",
                what_to_show="TRADES",
                use_rth=True,
            )
            if df is not None:
                logger.info(f"[{ticker}] Fetched {len(df)} 1-min bars")
            return df
        except Exception as exc:
            logger.error(f"[{ticker}] fetch_historical_data error: {exc}")
            return None

    # ── Statistics ────────────────────────────────────────────────────────────

    def _calc_stats(self, df: pd.DataFrame) -> Dict:
        close_col = "close" if "close" in df.columns else df.columns[-2]
        high_col  = "high"  if "high"  in df.columns else df.columns[1]
        low_col   = "low"   if "low"   in df.columns else df.columns[2]
        vol_col   = "volume" if "volume" in df.columns else df.columns[-1]

        return {
            "avg_price":   float(df[close_col].mean()),
            "volatility":  float(df[close_col].std()),
            "avg_range_pct": float(
                ((df[high_col] - df[low_col]) / df[close_col] * 100).mean()
            ),
            "avg_volume":  float(df[vol_col].mean()),
        }

    # ── Support / Resistance ──────────────────────────────────────────────────

    def _identify_levels(
        self, df: pd.DataFrame, current_price: float
    ) -> Tuple[List[PriceLevel], List[PriceLevel]]:

        high_col  = "high"  if "high"  in df.columns else df.columns[1]
        low_col   = "low"   if "low"   in df.columns else df.columns[2]

        # Try to resample to 5-min if we have a datetime index
        try:
            date_col = "date" if "date" in df.columns else None
            if date_col:
                df5 = df.set_index(date_col).resample("5min").agg(
                    {c: ("first" if c == "open" else
                         "max"  if c == "high"  else
                         "min"  if c == "low"   else
                         "last" if c == "close"  else
                         "sum")
                     for c in df.columns}
                ).dropna()
            else:
                df5 = df.copy()
        except Exception:
            df5 = df.copy()

        window = 10
        try:
            df5["_sh"] = df5[high_col] == df5[high_col].rolling(window, center=True).max()
            df5["_sl"] = df5[low_col]  == df5[low_col].rolling(window, center=True).min()
            swing_highs = df5.loc[df5["_sh"], high_col].values
            swing_lows  = df5.loc[df5["_sl"], low_col].values
        except Exception:
            swing_highs = np.array(df[high_col].nlargest(20).values)
            swing_lows  = np.array(df[low_col].nsmallest(20).values)

        res_clusters = self._cluster(swing_highs)
        sup_clusters = self._cluster(swing_lows)

        resistances = [
            obj for level in res_clusters
            for obj in [self._analyze_level(df5, level, "RESISTANCE", current_price, high_col, low_col)]
            if obj
        ]
        supports = [
            obj for level in sup_clusters
            for obj in [self._analyze_level(df5, level, "SUPPORT", current_price, high_col, low_col)]
            if obj
        ]

        resistances.sort(key=lambda x: x.strength, reverse=True)
        supports.sort(key=lambda x: x.strength, reverse=True)
        return supports[:5], resistances[:5]

    def _cluster(self, prices: np.ndarray, tolerance_pct: float = 0.5) -> List[float]:
        if len(prices) == 0:
            return []
        prices = np.sort(prices)
        clusters: List[float] = []
        bucket = [prices[0]]
        for p in prices[1:]:
            if (p - bucket[-1]) / bucket[-1] * 100 <= tolerance_pct:
                bucket.append(p)
            else:
                clusters.append(float(np.mean(bucket)))
                bucket = [p]
        clusters.append(float(np.mean(bucket)))
        return clusters

    def _analyze_level(
        self,
        df: pd.DataFrame,
        level: float,
        level_type: str,
        current_price: float,
        high_col: str = "high",
        low_col:  str = "low",
    ) -> Optional[PriceLevel]:
        tol = level * 0.003
        try:
            if level_type == "RESISTANCE":
                touches = df[df[high_col] >= level - tol]
            else:
                touches = df[df[low_col]  <= level + tol]
        except Exception:
            return None

        if len(touches) < 2:
            return None

        bounces = 0
        breaks  = 0
        for idx in touches.index:
            try:
                loc = df.index.get_loc(idx)
                if isinstance(loc, slice):
                    loc = loc.start
                if loc + 5 >= len(df):
                    continue
                future = df.iloc[loc + 1: loc + 6]
                close_col = "close" if "close" in df.columns else df.columns[-2]
                if level_type == "RESISTANCE":
                    if future[close_col].max() > level + tol:
                        breaks += 1
                    else:
                        bounces += 1
                else:
                    if future[close_col].min() < level - tol:
                        breaks += 1
                    else:
                        bounces += 1
            except Exception:
                continue

        total = bounces + breaks
        if total == 0:
            return None

        success_rate = bounces / total
        strength = (
            min(len(touches) * 10, 40)
            + (success_rate * 40)
            + (20 if abs(level - current_price) / current_price < 0.02 else 0)
        )

        last_ts = ""
        try:
            last_ts = str(touches.index[-1])
        except Exception:
            pass

        return PriceLevel(
            price=round(level, 2),
            level_type=level_type,
            touches=len(touches),
            bounces=bounces,
            breaks=breaks,
            last_interaction=last_ts,
            success_rate=round(success_rate, 2),
            strength=round(strength, 1),
        )

    # ── Pattern detection ──────────────────────────────────────────────────────

    def _identify_patterns(
        self,
        df: pd.DataFrame,
        supports: List[PriceLevel],
        resistances: List[PriceLevel],
    ) -> List[TradingPattern]:
        patterns = []
        for sup in supports[:3]:
            p = self._dip_bounce(df, sup.price)
            if p:
                patterns.append(p)
        for res in resistances[:3]:
            p = self._peak_reversal(df, res.price)
            if p:
                patterns.append(p)
        return patterns

    def _dip_bounce(self, df: pd.DataFrame, level: float) -> Optional[TradingPattern]:
        low_col   = "low"   if "low"   in df.columns else df.columns[2]
        close_col = "close" if "close" in df.columns else df.columns[-2]
        high_col  = "high"  if "high"  in df.columns else df.columns[1]
        tol = level * 0.005
        try:
            dips = df[df[low_col] <= level + tol]
        except Exception:
            return None
        if len(dips) < 3:
            return None

        gains, losses, durations = [], [], []
        for idx in dips.index:
            try:
                loc = df.index.get_loc(idx)
                if isinstance(loc, slice):
                    loc = loc.start
                entry = float(df.iloc[loc][close_col])
                future = df.iloc[loc + 1: loc + 61]
                if future.empty:
                    continue
                max_gain = (float(future[high_col].max()) - entry) / entry * 100
                max_loss = (float(future[low_col].min())  - entry) / entry * 100
                if max_gain >= 1.5:
                    gains.append(max_gain)
                    hits = future[future[high_col] >= entry * 1.015]
                    if not hits.empty:
                        try:
                            dur = (hits.index[0] - idx).total_seconds() / 60
                            durations.append(dur)
                        except Exception:
                            pass
                elif max_loss <= -2.5:
                    losses.append(max_loss)
            except Exception:
                continue

        total = len(gains) + len(losses)
        if total < 3:
            return None

        win_rate  = len(gains) / total
        avg_gain  = float(np.mean(gains))  if gains  else 0.0
        avg_loss  = float(np.mean(losses)) if losses else 0.0
        avg_dur   = int(np.mean(durations)) if durations else 0
        confidence = min(win_rate * 60 + min(total * 5, 30) + (10 if avg_gain > 2 else 0), 100)

        return TradingPattern(
            price_level=round(level, 2),
            pattern_type="DIP_BOUNCE",
            occurrences=total,
            avg_gain_pct=round(avg_gain, 2),
            avg_loss_pct=round(avg_loss, 2),
            win_rate=round(win_rate, 2),
            avg_duration_minutes=avg_dur,
            confidence=round(confidence, 1),
        )

    def _peak_reversal(self, df: pd.DataFrame, level: float) -> Optional[TradingPattern]:
        high_col  = "high"  if "high"  in df.columns else df.columns[1]
        low_col   = "low"   if "low"   in df.columns else df.columns[2]
        close_col = "close" if "close" in df.columns else df.columns[-2]
        tol = level * 0.005
        try:
            peaks = df[df[high_col] >= level - tol]
        except Exception:
            return None
        if len(peaks) < 3:
            return None

        gains, losses, durations = [], [], []
        for idx in peaks.index:
            try:
                loc = df.index.get_loc(idx)
                if isinstance(loc, slice):
                    loc = loc.start
                entry  = float(df.iloc[loc][close_col])
                future = df.iloc[loc + 1: loc + 61]
                if future.empty:
                    continue
                max_drop = (entry - float(future[low_col].min()))  / entry * 100
                max_rise = (float(future[high_col].max()) - entry) / entry * 100
                if max_drop >= 1.5:
                    gains.append(max_drop)
                    hits = future[future[low_col] <= entry * 0.985]
                    if not hits.empty:
                        try:
                            dur = (hits.index[0] - idx).total_seconds() / 60
                            durations.append(dur)
                        except Exception:
                            pass
                elif max_rise >= 2.5:
                    losses.append(-max_rise)
            except Exception:
                continue

        total = len(gains) + len(losses)
        if total < 3:
            return None

        win_rate   = len(gains) / total
        avg_gain   = float(np.mean(gains))  if gains  else 0.0
        avg_loss   = float(np.mean(losses)) if losses else 0.0
        avg_dur    = int(np.mean(durations)) if durations else 0
        confidence = min(win_rate * 60 + min(total * 5, 30) + (10 if avg_gain > 2 else 0), 100)

        return TradingPattern(
            price_level=round(level, 2),
            pattern_type="PEAK_REVERSAL",
            occurrences=total,
            avg_gain_pct=round(avg_gain, 2),
            avg_loss_pct=round(avg_loss, 2),
            win_rate=round(win_rate, 2),
            avg_duration_minutes=avg_dur,
            confidence=round(confidence, 1),
        )

    # ── Nearest levels ─────────────────────────────────────────────────────────

    def _find_nearest(
        self,
        price: float,
        supports: List[PriceLevel],
        resistances: List[PriceLevel],
    ) -> Tuple[Optional[float], Optional[float]]:
        below = [s.price for s in supports    if s.price < price]
        above = [r.price for r in resistances if r.price > price]
        return (max(below) if below else None, min(above) if above else None)

    # ── Recommendation ────────────────────────────────────────────────────────

    def _recommendation(
        self,
        current_price: float,
        near_sup: Optional[float],
        near_res: Optional[float],
        dist_sup: float,
        dist_res: float,
        patterns: List[TradingPattern],
        stats: Dict,
    ) -> Tuple[float, float, bool, str, str]:

        long_conf  = 0.0
        short_conf = 0.0

        if near_sup and dist_sup < 2:
            long_conf += 30
            for p in patterns:
                if p.pattern_type == "DIP_BOUNCE" and abs(p.price_level - near_sup) / near_sup < 0.01:
                    long_conf += p.confidence * 0.7
                    break

        if near_res and dist_res < 2:
            short_conf += 30
            for p in patterns:
                if p.pattern_type == "PEAK_REVERSAL" and abs(p.price_level - near_res) / near_res < 0.01:
                    short_conf += p.confidence * 0.7
                    break

        # Penalise mid-range trading
        if dist_sup > 5 and dist_res > 5:
            long_conf  *= 0.5
            short_conf *= 0.5

        long_conf  = round(min(long_conf,  100.0), 1)
        short_conf = round(min(short_conf, 100.0), 1)

        thr = self.min_confidence_threshold
        if long_conf >= thr:
            return (
                long_conf, short_conf, True, "LONG",
                f"Near support ${near_sup:.2f} ({dist_sup:.1f}% below), DIP_BOUNCE pattern detected",
            )
        if short_conf >= thr:
            return (
                long_conf, short_conf, True, "SHORT",
                f"Near resistance ${near_res:.2f} ({dist_res:.1f}% above), PEAK_REVERSAL pattern detected",
            )
        if long_conf > 30 or short_conf > 30:
            return (
                long_conf, short_conf, False, "WAIT",
                f"Confidence too low (L:{long_conf:.0f}% S:{short_conf:.0f}%), need {thr:.0f}%+",
            )
        return (
            long_conf, short_conf, False, "AVOID",
            "No clear pattern — price not near significant support/resistance",
        )

    # ── Cache helpers ──────────────────────────────────────────────────────────

    def _save_cache(self, analysis: TickerAnalysis):
        try:
            path = self.cache_dir / f"{analysis.ticker}_{datetime.now().strftime('%Y%m%d')}.json"
            with open(path, "w") as f:
                json.dump(analysis.to_dict(), f, indent=2, default=str)
        except Exception as exc:
            logger.warning(f"Cache save failed: {exc}")

    def _empty_analysis(self, ticker: str, current_price: float) -> TickerAnalysis:
        return TickerAnalysis(
            ticker=ticker,
            analysis_date=datetime.now().isoformat(),
            days_analyzed=0,
            data_points=0,
            avg_price=current_price,
            price_volatility=0.0,
            avg_daily_range_pct=0.0,
            avg_volume=0,
            support_levels=[],
            resistance_levels=[],
            patterns=[],
            current_price=current_price,
            nearest_support=None,
            nearest_resistance=None,
            distance_to_support_pct=999.0,
            distance_to_resistance_pct=999.0,
            long_confidence=0.0,
            short_confidence=0.0,
            should_trade=False,
            recommendation="AVOID",
            reason="Insufficient historical data",
        )
