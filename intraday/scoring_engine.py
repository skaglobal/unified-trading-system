"""
Intraday Scoring Engine
========================
Converts an ``IndicatorSnapshot`` into a normalised Long/Short probability
score (0–100) plus a human-readable confidence label and reason list.

Weights are loaded from ``config/intraday_scoring.yaml`` and can be
overridden at runtime without touching this file.

Scoring method
--------------
For each directional "factor" that fires, the engine accumulates the
configured weight into the corresponding pool (long or short).  At the end
the raw pool values are converted to a 0–100 scale using:

    score = 100 * raw_pool / max_possible_pool

where ``max_possible_pool`` is the sum of ALL weights on that side (i.e.
the score if every single bullish / bearish factor fired simultaneously).

A "no trade" zone is applied when |long - short| < `thresholds.no_trade_gap`.

DISCLAIMER: Output is probability guidance, not guaranteed prediction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from intraday.indicator_engine import IndicatorSnapshot

logger = logging.getLogger("intraday.scoring_engine")

# Default config path (relative to project root)
_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "intraday_scoring.yaml"


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass
class ScoredFactor:
    """A single factor that contributed to the score."""
    name: str
    side: str          # "long" | "short" | "both"
    weight: float
    fired: bool
    description: str


@dataclass
class ScoreResult:
    """Complete scoring output for one refresh cycle."""

    symbol: str

    long_score: float   = 0.0   # 0–100
    short_score: float  = 0.0   # 0–100

    confidence_label: str = "No Trade"
    # "Strong Long" | "Weak Long" | "No Trade" | "Weak Short" | "Strong Short"

    dominant_side: str = "none"   # "long" | "short" | "none"

    long_reasons: List[str]  = field(default_factory=list)
    short_reasons: List[str] = field(default_factory=list)
    neutral_notes: List[str] = field(default_factory=list)

    factors: List[ScoredFactor] = field(default_factory=list)

    # Warning flags
    rsi_overbought: bool = False
    rsi_oversold: bool   = False
    reversal_risk: bool  = False   # set by AlertEngine


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ScoringEngine:
    """Compute Long/Short probability scores from an IndicatorSnapshot.

    Args:
        config_path: Path to ``intraday_scoring.yaml``.  Defaults to
                     ``config/intraday_scoring.yaml`` in the project root.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        path = config_path or _DEFAULT_CONFIG
        self._cfg = self._load_config(path)
        self._factors_cfg: Dict[str, Any] = self._cfg.get("scoring", {})
        self._thresholds: Dict[str, Any] = self._cfg.get("thresholds", {})
        logger.info("ScoringEngine loaded config from %s", path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, snap: IndicatorSnapshot) -> ScoreResult:
        """Compute a ``ScoreResult`` from an ``IndicatorSnapshot``."""
        result = ScoreResult(symbol=snap.symbol)

        factors: List[ScoredFactor] = []
        long_pool: float = 0.0
        short_pool: float = 0.0
        long_max: float = 0.0
        short_max: float = 0.0

        t = self._thresholds

        # ── Evaluate each factor ─────────────────────────────────────────
        evaluations: List[Tuple[str, bool, str]] = self._evaluate_factors(snap, t)
        # evaluations: list of (factor_name, fired, side)

        for name, fired, description in evaluations:
            cfg = self._factors_cfg.get(name, {})
            weight = float(cfg.get("weight", 1.0))
            side = str(cfg.get("side", "long"))

            sf = ScoredFactor(name=name, side=side, weight=weight, fired=fired, description=description)
            factors.append(sf)

            if side == "long":
                long_max += weight
                if fired:
                    long_pool += weight
            elif side == "short":
                short_max += weight
                if fired:
                    short_pool += weight
            elif side == "both":
                # Volume / spread factors boost the dominant direction
                long_max += weight
                short_max += weight
                if fired:
                    long_pool += weight
                    short_pool += weight

        result.factors = factors

        # ── Normalise to 0–100 ───────────────────────────────────────────
        result.long_score  = 100.0 * long_pool  / long_max  if long_max  > 0 else 0.0
        result.short_score = 100.0 * short_pool / short_max if short_max > 0 else 0.0

        # ── Confidence label ─────────────────────────────────────────────
        no_trade_gap  = float(t.get("no_trade_gap",  15))
        strong_thresh = float(t.get("strong_threshold", 68))
        weak_thresh   = float(t.get("weak_threshold",   50))

        gap = abs(result.long_score - result.short_score)
        if gap < no_trade_gap:
            result.confidence_label = "No Trade"
            result.dominant_side = "none"
        elif result.long_score >= result.short_score:
            result.dominant_side = "long"
            if result.long_score >= strong_thresh:
                result.confidence_label = "Strong Long"
            elif result.long_score >= weak_thresh:
                result.confidence_label = "Weak Long"
            else:
                result.confidence_label = "No Trade"
        else:
            result.dominant_side = "short"
            if result.short_score >= strong_thresh:
                result.confidence_label = "Strong Short"
            elif result.short_score >= weak_thresh:
                result.confidence_label = "Weak Short"
            else:
                result.confidence_label = "No Trade"

        # ── Collect human-readable reasons ───────────────────────────────
        result.long_reasons = [
            sf.description for sf in factors
            if sf.fired and sf.side == "long"
        ]
        result.short_reasons = [
            sf.description for sf in factors
            if sf.fired and sf.side == "short"
        ]

        # Volume / dual-side fired factors
        result.neutral_notes = [
            sf.description for sf in factors
            if sf.fired and sf.side == "both"
        ]

        # ── Warning flags ────────────────────────────────────────────────
        rsi_overbought = float(t.get("rsi_overbought", 75))
        rsi_oversold   = float(t.get("rsi_oversold",   25))
        if snap.rsi14 is not None:
            result.rsi_overbought = snap.rsi14 >= rsi_overbought
            result.rsi_oversold   = snap.rsi14 <= rsi_oversold

        return result

    # ------------------------------------------------------------------
    # Factor evaluation
    # ------------------------------------------------------------------

    def _evaluate_factors(
        self, snap: IndicatorSnapshot, t: Dict[str, Any]
    ) -> List[Tuple[str, bool, str]]:
        """Evaluate every factor and return (name, fired, description) triples."""
        price = snap.latest_close
        results: List[Tuple[str, bool, str]] = []

        def _ev(name: str, fired: bool) -> None:
            desc = self._factors_cfg.get(name, {}).get("description", name)
            results.append((name, fired, desc))

        # ── VWAP ────────────────────────────────────────────────────────
        if snap.vwap:
            _ev("price_above_vwap", price > snap.vwap)
            _ev("price_below_vwap", price < snap.vwap)

        # ── SMAs ────────────────────────────────────────────────────────
        if snap.sma20 and snap.sma50:
            _ev("sma20_above_sma50", snap.sma20 > snap.sma50)
            _ev("sma20_below_sma50", snap.sma20 < snap.sma50)
        if snap.sma20:
            _ev("price_above_sma20", price > snap.sma20)
            _ev("price_below_sma20", price < snap.sma20)
        if snap.sma50:
            _ev("price_above_sma50", price > snap.sma50)
            _ev("price_below_sma50", price < snap.sma50)

        # ── SMA50 slope ──────────────────────────────────────────────────
        if snap.sma50_slope is not None:
            _ev("sma50_slope_positive", snap.sma50_slope > 0)
            _ev("sma50_slope_negative", snap.sma50_slope < 0)

        # ── RSI ──────────────────────────────────────────────────────────
        if snap.rsi14 is not None:
            rsi_bull_lo = float(t.get("rsi_bull_low",  55))
            rsi_bull_hi = float(t.get("rsi_bull_high", 72))
            rsi_bear_lo = float(t.get("rsi_bear_low",  28))
            rsi_bear_hi = float(t.get("rsi_bear_high", 45))
            _ev("rsi_bullish_zone", rsi_bull_lo <= snap.rsi14 <= rsi_bull_hi)
            _ev("rsi_bearish_zone", rsi_bear_lo <= snap.rsi14 <= rsi_bear_hi)

        # ── MACD ─────────────────────────────────────────────────────────
        if snap.macd_line is not None and snap.macd_signal is not None:
            _ev("macd_bullish", snap.macd_line > snap.macd_signal)
            _ev("macd_bearish", snap.macd_line < snap.macd_signal)
        if snap.macd_hist is not None and snap.macd_hist_prev is not None:
            _ev("macd_hist_rising",  snap.macd_hist > snap.macd_hist_prev)
            _ev("macd_hist_falling", snap.macd_hist < snap.macd_hist_prev)

        # ── Volume ───────────────────────────────────────────────────────
        if snap.rel_volume is not None:
            vol_spike   = float(t.get("vol_spike_ratio",   1.5))
            vol_extreme = float(t.get("vol_extreme_ratio", 2.5))
            _ev("volume_spike",   snap.rel_volume >= vol_spike)
            _ev("volume_extreme", snap.rel_volume >= vol_extreme)

        # ── Bid-ask imbalance ────────────────────────────────────────────
        if snap.bid_ask_imbalance is not None:
            imb_thresh = float(t.get("imbalance_threshold", 0.60))
            _ev("bid_ask_bullish", snap.bid_ask_imbalance >= imb_thresh)
            _ev("bid_ask_bearish", snap.bid_ask_imbalance <= (1.0 - imb_thresh))

        # ── Spread ───────────────────────────────────────────────────────
        if snap.spread_pct is not None:
            _ev("tight_spread", snap.spread_pct < 0.05)  # < 5 bps

        # ── Breakout / breakdown ─────────────────────────────────────────
        _ev("breakout_above_resistance", snap.breakout)
        _ev("breakdown_below_support",   snap.breakdown)

        # ── Candle structure ─────────────────────────────────────────────
        _ev("bullish_candle", snap.candle_bullish)
        _ev("bearish_candle", not snap.candle_bullish)

        wick_thresh = float(t.get("wick_ratio_threshold", 0.5))
        if snap.lower_wick_pct is not None:
            _ev("large_lower_wick_long", snap.lower_wick_pct >= wick_thresh)
        if snap.upper_wick_pct is not None:
            _ev("large_upper_wick_short", snap.upper_wick_pct >= wick_thresh)

        return results

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config(path: Path) -> Dict[str, Any]:
        try:
            with open(path, "r") as fh:
                return yaml.safe_load(fh) or {}
        except FileNotFoundError:
            logger.warning("Scoring config not found at %s — using empty config", path)
            return {}
        except yaml.YAMLError as exc:
            logger.error("YAML parse error in %s: %s", path, exc)
            return {}

    def reload_config(self, config_path: Optional[Path] = None) -> None:
        """Hot-reload scoring weights from disk."""
        path = config_path or _DEFAULT_CONFIG
        self._cfg = self._load_config(path)
        self._factors_cfg = self._cfg.get("scoring", {})
        self._thresholds   = self._cfg.get("thresholds", {})
        logger.info("ScoringEngine config reloaded")
