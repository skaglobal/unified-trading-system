"""
Intraday Guidance Engine
=========================
Translates a ``ScoreResult`` + ``IndicatorSnapshot`` into concrete,
actionable trade parameters and exit warnings.

Outputs
-------
- Potential long entry price
- Potential short entry price
- Suggested stop loss
- Suggested target 1 and target 2
- Estimated reward / risk ratio
- Exit warning when conditions weaken

DISCLAIMER: All output is probability guidance, not guaranteed prediction.
            This module is for educational decision support only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from intraday.indicator_engine import IndicatorSnapshot
from intraday.scoring_engine import ScoreResult

logger = logging.getLogger("intraday.guidance_engine")


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

@dataclass
class GuidanceResult:
    """Actionable trade guidance for one refresh cycle."""

    symbol: str
    direction: str      # "long" | "short" | "none"

    # ── Entry ─────────────────────────────────────────────────────────────
    suggested_entry: Optional[float] = None   # potential entry price
    entry_basis: str = ""                     # human explanation

    # ── Risk management ───────────────────────────────────────────────────
    stop_loss: Optional[float] = None
    stop_basis: str = ""

    target1: Optional[float] = None
    target2: Optional[float] = None
    reward_risk: Optional[float] = None   # (T1 - entry) / (entry - stop)

    # ── Exit warning ──────────────────────────────────────────────────────
    exit_warning: bool = False
    exit_warning_reason: str = ""

    # ── Human-readable summary ────────────────────────────────────────────
    summary_lines: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class GuidanceEngine:
    """Generate trade guidance from scoring and indicator data.

    Args:
        stop_atr_mult:    ATR multiplier for stop distance.
        target1_atr_mult: ATR multiplier for first target.
        target2_atr_mult: ATR multiplier for second target.
        exit_drop_pct:    Fraction by which the dominant score must fall
                          before an exit warning is raised (vs. prev cycle).
    """

    def __init__(
        self,
        stop_atr_mult: float = 1.0,
        target1_atr_mult: float = 1.5,
        target2_atr_mult: float = 2.5,
        exit_drop_pct: float = 0.15,
    ) -> None:
        self.stop_atr_mult    = stop_atr_mult
        self.target1_atr_mult = target1_atr_mult
        self.target2_atr_mult = target2_atr_mult
        self.exit_drop_pct    = exit_drop_pct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_guidance(
        self,
        score: ScoreResult,
        snap: IndicatorSnapshot,
        prev_score: Optional[ScoreResult] = None,
    ) -> GuidanceResult:
        """Compute guidance from the latest score + indicator snapshot.

        Args:
            score:      Current ``ScoreResult``.
            snap:       Current ``IndicatorSnapshot``.
            prev_score: Previous cycle's ``ScoreResult`` (for exit warnings).
        """
        guidance = GuidanceResult(symbol=snap.symbol, direction=score.dominant_side)

        price  = snap.latest_close
        atr    = snap.atr14

        if score.dominant_side == "none" or score.confidence_label == "No Trade":
            guidance.summary_lines = [
                "⚖️  No clear directional edge — conditions are mixed.",
                "📌  Wait for cleaner setup before entering.",
            ]
            return guidance

        direction = score.dominant_side  # "long" or "short"

        # ── Entry ─────────────────────────────────────────────────────────
        entry = self._calc_entry(direction, price, snap)
        guidance.suggested_entry = entry
        guidance.entry_basis = self._entry_basis(direction, snap)

        # ── Stop loss ─────────────────────────────────────────────────────
        stop, stop_basis = self._calc_stop(direction, entry, snap, atr)
        guidance.stop_loss = stop
        guidance.stop_basis = stop_basis

        # ── Targets ───────────────────────────────────────────────────────
        if entry and stop:
            risk = abs(entry - stop)
            if direction == "long":
                if atr:
                    guidance.target1 = round(entry + atr * self.target1_atr_mult, 4)
                    guidance.target2 = round(entry + atr * self.target2_atr_mult, 4)
                else:
                    guidance.target1 = round(entry + risk * 1.5, 4)
                    guidance.target2 = round(entry + risk * 2.5, 4)
                t1 = guidance.target1
            else:
                if atr:
                    guidance.target1 = round(entry - atr * self.target1_atr_mult, 4)
                    guidance.target2 = round(entry - atr * self.target2_atr_mult, 4)
                else:
                    guidance.target1 = round(entry - risk * 1.5, 4)
                    guidance.target2 = round(entry - risk * 2.5, 4)
                t1 = guidance.target1

            if risk > 0 and t1 is not None:
                guidance.reward_risk = round(abs(t1 - entry) / risk, 2)

        # ── Exit warning ──────────────────────────────────────────────────
        self._check_exit_warning(guidance, score, prev_score, snap)

        # ── Summary lines ─────────────────────────────────────────────────
        guidance.summary_lines = self._build_summary(guidance, score, snap)

        return guidance

    # ------------------------------------------------------------------
    # Entry calculation
    # ------------------------------------------------------------------

    def _calc_entry(
        self, direction: str, price: float, snap: IndicatorSnapshot
    ) -> float:
        """Suggested entry: current price for market orders (educational)."""
        if direction == "long":
            # Prefer asking price for market fills; fall back to last
            return snap.bid_ask_imbalance and price or price
        else:
            return price

    def _entry_basis(self, direction: str, snap: IndicatorSnapshot) -> str:
        if direction == "long":
            if snap.breakout and snap.nearest_resistance:
                return f"Breakout above resistance ~{snap.nearest_resistance:.2f}"
            if snap.vwap and snap.latest_close > snap.vwap:
                return f"Long at current price above VWAP ({snap.vwap:.2f})"
            return "Long at market on bullish confluence"
        else:
            if snap.breakdown and snap.nearest_support:
                return f"Breakdown below support ~{snap.nearest_support:.2f}"
            if snap.vwap and snap.latest_close < snap.vwap:
                return f"Short at current price below VWAP ({snap.vwap:.2f})"
            return "Short at market on bearish confluence"

    # ------------------------------------------------------------------
    # Stop calculation
    # ------------------------------------------------------------------

    def _calc_stop(
        self,
        direction: str,
        entry: float,
        snap: IndicatorSnapshot,
        atr: Optional[float],
    ) -> tuple:
        """Stop loss: uses ATR or nearest S/R level, whichever is closer."""
        atr_stop: Optional[float] = None
        sr_stop:  Optional[float] = None
        basis: str = ""

        if atr:
            if direction == "long":
                atr_stop = round(entry - atr * self.stop_atr_mult, 4)
            else:
                atr_stop = round(entry + atr * self.stop_atr_mult, 4)

        if direction == "long" and snap.nearest_support:
            sr_stop = round(snap.nearest_support * 0.998, 4)  # just below support
            basis = f"Just below nearest support ~{snap.nearest_support:.2f}"
        elif direction == "short" and snap.nearest_resistance:
            sr_stop = round(snap.nearest_resistance * 1.002, 4)  # just above resistance
            basis = f"Just above nearest resistance ~{snap.nearest_resistance:.2f}"

        # Choose: use ATR if no S/R, else use whichever gives a tighter stop
        if atr_stop is None and sr_stop is None:
            return None, ""
        if atr_stop is None:
            return sr_stop, basis
        if sr_stop is None:
            return atr_stop, f"ATR({self.stop_atr_mult:.1f}×) stop"

        # Pick the closer stop (smaller risk)
        if direction == "long":
            stop = max(atr_stop, sr_stop)  # closest to entry = largest value
            basis = f"ATR stop ({atr_stop:.2f}) vs S/R stop ({sr_stop:.2f}) → using {stop:.2f}"
        else:
            stop = min(atr_stop, sr_stop)
            basis = f"ATR stop ({atr_stop:.2f}) vs S/R stop ({sr_stop:.2f}) → using {stop:.2f}"

        return stop, basis

    # ------------------------------------------------------------------
    # Exit warning
    # ------------------------------------------------------------------

    def _check_exit_warning(
        self,
        guidance: GuidanceResult,
        score: ScoreResult,
        prev_score: Optional[ScoreResult],
        snap: IndicatorSnapshot,
    ) -> None:
        reasons: List[str] = []

        # Score degraded significantly vs. previous cycle
        if prev_score and score.dominant_side != "none":
            if score.dominant_side == "long":
                drop = prev_score.long_score - score.long_score
            else:
                drop = prev_score.short_score - score.short_score
            if drop >= self.exit_drop_pct * 100:
                reasons.append(f"Score dropped {drop:.0f} pts — momentum fading")

        # RSI extremes
        if score.rsi_overbought and score.dominant_side == "long":
            reasons.append("RSI overbought — potential reversal risk")
        if score.rsi_oversold and score.dominant_side == "short":
            reasons.append("RSI oversold — potential bounce risk")

        # Confidence label weakening
        if score.confidence_label in ("Weak Long", "Weak Short"):
            reasons.append("Confidence is only 'Weak' — consider reducing size or tightening stop")

        # No Trade boundary
        if abs(score.long_score - score.short_score) < 20:
            reasons.append("Long/Short scores converging — setup losing clarity")

        if reasons:
            guidance.exit_warning = True
            guidance.exit_warning_reason = " | ".join(reasons)

    # ------------------------------------------------------------------
    # Summary text
    # ------------------------------------------------------------------

    def _build_summary(
        self,
        guidance: GuidanceResult,
        score: ScoreResult,
        snap: IndicatorSnapshot,
    ) -> List[str]:
        lines: List[str] = []
        d = guidance.direction

        label_emoji = {
            "Strong Long": "🟢🟢", "Weak Long": "🟢",
            "Strong Short": "🔴🔴", "Weak Short": "🔴", "No Trade": "⚪",
        }
        emoji = label_emoji.get(score.confidence_label, "⚪")

        lines.append(f"{emoji}  **{score.confidence_label}** — "
                     f"Long {score.long_score:.0f}  |  Short {score.short_score:.0f}")

        if guidance.suggested_entry:
            lines.append(f"📍 Entry: **{guidance.suggested_entry:.2f}**  ({guidance.entry_basis})")
        if guidance.stop_loss:
            lines.append(f"🛑 Stop: **{guidance.stop_loss:.2f}**  ({guidance.stop_basis})")
        if guidance.target1:
            lines.append(f"🎯 T1: **{guidance.target1:.2f}**")
        if guidance.target2:
            lines.append(f"🎯 T2: **{guidance.target2:.2f}**")
        if guidance.reward_risk:
            lines.append(f"⚖️  Est. R/R (to T1): **{guidance.reward_risk:.2f}x**")

        if guidance.exit_warning:
            lines.append(f"⚠️  Exit warning: {guidance.exit_warning_reason}")

        if score.rsi_overbought:
            lines.append("⚠️  RSI overbought — be cautious adding longs")
        if score.rsi_oversold:
            lines.append("⚠️  RSI oversold — be cautious adding shorts")

        lines.append("")
        lines.append("*All levels are probability guidance — not guaranteed signals.*")

        return lines
