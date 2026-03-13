"""
Intraday Alert Engine
======================
Generates structured alerts from scoring and indicator snapshots.

Alert types
-----------
- LONG_SETUP    : Long setup detected above threshold
- SHORT_SETUP   : Short setup detected above threshold
- EXIT_LONG     : Long setup weakening — consider exiting
- EXIT_SHORT    : Short setup weakening — consider exiting
- NO_TRADE      : Choppy / no clear edge
- REVERSAL_RISK : Opposing score rising — possible reversal

DISCLAIMER: For educational decision support only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import List, Optional

from intraday.indicator_engine import IndicatorSnapshot
from intraday.scoring_engine import ScoreResult

logger = logging.getLogger("intraday.alert_engine")


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

class AlertType(str, Enum):
    LONG_SETUP    = "LONG_SETUP"
    SHORT_SETUP   = "SHORT_SETUP"
    EXIT_LONG     = "EXIT_LONG"
    EXIT_SHORT    = "EXIT_SHORT"
    NO_TRADE      = "NO_TRADE"
    REVERSAL_RISK = "REVERSAL_RISK"


@dataclass
class Alert:
    """A single alert event."""
    alert_type: AlertType
    symbol: str
    message: str
    timestamp: datetime = None

    long_score: float = 0.0
    short_score: float = 0.0
    confidence_label: str = ""

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()

    @property
    def emoji(self) -> str:
        return {
            AlertType.LONG_SETUP:    "🟢",
            AlertType.SHORT_SETUP:   "🔴",
            AlertType.EXIT_LONG:     "⚠️",
            AlertType.EXIT_SHORT:    "⚠️",
            AlertType.NO_TRADE:      "⚪",
            AlertType.REVERSAL_RISK: "🔄",
        }.get(self.alert_type, "ℹ️")

    @property
    def formatted(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S") if self.timestamp else "–"
        return f"{self.emoji} [{ts}] {self.alert_type.value} | {self.symbol} | {self.message}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AlertEngine:
    """Generate alerts by comparing current vs. previous scoring cycles.

    Args:
        long_min_score:       Minimum long score to trigger LONG_SETUP alert.
        short_min_score:      Minimum short score to trigger SHORT_SETUP alert.
        exit_score_drop:      Score drop (pts) that triggers EXIT alert.
        reversal_risk_gap:    Gap in scores that signals reversal risk.
        max_history:          Maximum alerts to retain in the history buffer.
    """

    def __init__(
        self,
        long_min_score: float = 65.0,
        short_min_score: float = 65.0,
        exit_score_drop: float = 15.0,
        reversal_risk_gap: float = 20.0,
        max_history: int = 50,
    ) -> None:
        self.long_min_score  = long_min_score
        self.short_min_score = short_min_score
        self.exit_score_drop = exit_score_drop
        self.reversal_risk_gap = reversal_risk_gap
        self.max_history = max_history

        self._history: List[Alert] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_alerts(
        self,
        score: ScoreResult,
        snap: IndicatorSnapshot,
        prev_score: Optional[ScoreResult] = None,
    ) -> List[Alert]:
        """Evaluate current score vs. previous and return new alerts."""
        alerts: List[Alert] = []

        # ── Long setup ────────────────────────────────────────────────────
        if score.long_score >= self.long_min_score and score.dominant_side == "long":
            alerts.append(Alert(
                alert_type=AlertType.LONG_SETUP,
                symbol=snap.symbol,
                message=(
                    f"Long score {score.long_score:.0f} — "
                    f"{score.confidence_label} | "
                    + ("; ".join(score.long_reasons[:3]) or "bullish confluence")
                ),
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label=score.confidence_label,
            ))

        # ── Short setup ───────────────────────────────────────────────────
        elif score.short_score >= self.short_min_score and score.dominant_side == "short":
            alerts.append(Alert(
                alert_type=AlertType.SHORT_SETUP,
                symbol=snap.symbol,
                message=(
                    f"Short score {score.short_score:.0f} — "
                    f"{score.confidence_label} | "
                    + ("; ".join(score.short_reasons[:3]) or "bearish confluence")
                ),
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label=score.confidence_label,
            ))

        # ── No trade / chop ───────────────────────────────────────────────
        if score.confidence_label == "No Trade":
            alerts.append(Alert(
                alert_type=AlertType.NO_TRADE,
                symbol=snap.symbol,
                message="No clear edge — long/short scores are close. Avoid entering.",
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label="No Trade",
            ))

        # ── Exit warnings (requires previous cycle) ───────────────────────
        if prev_score:
            self._check_exits(alerts, score, prev_score, snap)
            self._check_reversal_risk(alerts, score, prev_score, snap)

        # Store and cap history
        self._history.extend(alerts)
        if len(self._history) > self.max_history:
            self._history = self._history[-self.max_history :]

        return alerts

    @property
    def history(self) -> List[Alert]:
        """All alerts generated since the engine was created (capped at max_history)."""
        return list(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_exits(
        self,
        alerts: List[Alert],
        score: ScoreResult,
        prev_score: ScoreResult,
        snap: IndicatorSnapshot,
    ) -> None:
        """Emit EXIT alerts when the dominant score drops significantly."""
        # Exit long
        if (prev_score.dominant_side == "long"
                and (prev_score.long_score - score.long_score) >= self.exit_score_drop):
            alerts.append(Alert(
                alert_type=AlertType.EXIT_LONG,
                symbol=snap.symbol,
                message=(
                    f"Long score fell from {prev_score.long_score:.0f} → "
                    f"{score.long_score:.0f}. Bullish momentum weakening."
                ),
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label=score.confidence_label,
            ))

        # Exit short
        if (prev_score.dominant_side == "short"
                and (prev_score.short_score - score.short_score) >= self.exit_score_drop):
            alerts.append(Alert(
                alert_type=AlertType.EXIT_SHORT,
                symbol=snap.symbol,
                message=(
                    f"Short score fell from {prev_score.short_score:.0f} → "
                    f"{score.short_score:.0f}. Bearish momentum weakening."
                ),
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label=score.confidence_label,
            ))

    def _check_reversal_risk(
        self,
        alerts: List[Alert],
        score: ScoreResult,
        prev_score: ScoreResult,
        snap: IndicatorSnapshot,
    ) -> None:
        """Emit REVERSAL_RISK when the opposing score is rising rapidly."""
        # Was long, now short score surging
        if (prev_score.dominant_side == "long"
                and (score.short_score - prev_score.short_score) >= self.reversal_risk_gap):
            alerts.append(Alert(
                alert_type=AlertType.REVERSAL_RISK,
                symbol=snap.symbol,
                message=(
                    f"Short score jumped {prev_score.short_score:.0f} → "
                    f"{score.short_score:.0f} — possible bearish reversal forming."
                ),
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label=score.confidence_label,
            ))

        # Was short, now long score surging
        if (prev_score.dominant_side == "short"
                and (score.long_score - prev_score.long_score) >= self.reversal_risk_gap):
            alerts.append(Alert(
                alert_type=AlertType.REVERSAL_RISK,
                symbol=snap.symbol,
                message=(
                    f"Long score jumped {prev_score.long_score:.0f} → "
                    f"{score.long_score:.0f} — possible bullish reversal forming."
                ),
                long_score=score.long_score,
                short_score=score.short_score,
                confidence_label=score.confidence_label,
            ))
