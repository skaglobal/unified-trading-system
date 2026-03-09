"""
AI Insights Dashboard Page

Rule-based signal engine that analyses technical indicators and produces:
  - Trade suggestions with direction (Long / Short), entry, stop-loss and
    take-profit levels
  - Human-readable trade narratives
  - Pattern-recognition insights
  - Market commentary
  - Risk warnings
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analysis.indicators import TechnicalIndicators
from connectors.yahoo_finance import YahooFinanceConnector
from core.logging_manager import get_logger

logger = get_logger("dashboard.ai_insights")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WATCHLIST: List[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    # Broad tech / semis
    "AMD", "INTC", "QCOM", "AVGO", "MU", "ADBE", "CRM", "NOW",
    # Financials
    "JPM", "GS", "BAC", "V", "MA",
    # Healthcare / biotech
    "UNH", "JNJ", "LLY",
    # Energy / industrials
    "XOM", "CAT", "HON",
    # Consumer
    "COST", "WMT", "HD",
    # ETFs – broad market
    "SPY", "QQQ", "IWM",
]

SECTOR_MAP: Dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Semiconductors",
    "GOOGL": "Technology", "META": "Technology", "AMZN": "Consumer Disc.",
    "TSLA": "Consumer Disc.", "AMD": "Semiconductors", "INTC": "Semiconductors",
    "QCOM": "Semiconductors", "AVGO": "Semiconductors", "MU": "Semiconductors",
    "ADBE": "Technology", "CRM": "Technology", "NOW": "Technology",
    "JPM": "Financials", "GS": "Financials", "BAC": "Financials",
    "V": "Financials", "MA": "Financials",
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "XOM": "Energy", "CAT": "Industrials", "HON": "Industrials",
    "COST": "Consumer Staples", "WMT": "Consumer Staples", "HD": "Consumer Disc.",
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF",
}

ATR_STOP_MULT: float = 1.5   # stop = entry ± ATR × 1.5
ATR_TARGET_MULT: float = 3.5  # target = entry ± ATR × 3.5  (≈ 2.3 R/R)
MIN_DATA_ROWS: int = 60       # minimum bars needed to compute all indicators


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TradeSignal:
    symbol: str
    direction: str          # "LONG" | "SHORT"
    confidence: float       # 0–100
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    rsi: float
    atr: float
    volume_ratio: float
    macd_bullish: bool
    ma_aligned: bool
    narrative: str
    patterns: List[str] = field(default_factory=list)
    sector: str = "Unknown"

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_per_share(self) -> float:
        return abs(self.take_profit - self.entry)


@dataclass
class MarketRegime:
    label: str       # "Bull" | "Bear" | "Neutral" | "Choppy"
    score: int       # -100 to +100
    drivers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------

class AISignalEngine:
    """
    Analyses OHLCV data with technical indicators and produces structured
    trade signals.  No external LLM or API key required.
    """

    def __init__(self) -> None:
        self.indicators = TechnicalIndicators()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def generate_signals(
        self, raw_data: Dict[str, pd.DataFrame]
    ) -> List[TradeSignal]:
        """
        Generate trade signals for all symbols that pass the data-quality
        filter.  Returns a list sorted by confidence (descending).
        """
        signals: List[TradeSignal] = []

        for symbol, df in raw_data.items():
            if df is None or df.empty or len(df) < MIN_DATA_ROWS:
                continue

            try:
                enriched = self.indicators.add_all_indicators(df)
                sig = self._score_symbol(symbol, enriched)
                if sig is not None:
                    signals.append(sig)
            except Exception as exc:
                logger.warning(f"Signal error for {symbol}: {exc}")

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals

    def analyse_market_regime(
        self, index_data: Dict[str, pd.DataFrame]
    ) -> MarketRegime:
        """
        Derive a broad market-regime score from SPY / QQQ / IWM.
        """
        score = 0
        drivers: List[str] = []

        for sym, df in index_data.items():
            if df is None or df.empty or len(df) < MIN_DATA_ROWS:
                continue
            try:
                enriched = self.indicators.add_all_indicators(df)
                row = enriched.iloc[-1]

                close = row.get("Close", float("nan"))
                sma20 = row.get("SMA_20", float("nan"))
                sma50 = row.get("SMA_50", float("nan"))
                sma200 = row.get("SMA_200", float("nan"))
                rsi = row.get("RSI_14", float("nan"))
                macd = row.get("MACD", float("nan"))
                macd_sig = row.get("MACD_Signal", float("nan"))

                if not any(math.isnan(v) for v in [close, sma20, sma50, sma200]):
                    if close > sma20 > sma50 > sma200:
                        score += 20
                        drivers.append(f"{sym}: Full bullish MA stack")
                    elif close < sma20 < sma50 < sma200:
                        score -= 20
                        drivers.append(f"{sym}: Full bearish MA stack")
                    elif close > sma50:
                        score += 10
                    else:
                        score -= 10

                if not math.isnan(rsi):
                    if 50 < rsi < 70:
                        score += 10
                    elif rsi > 70:
                        score += 5
                        drivers.append(f"{sym}: RSI overbought ({rsi:.0f})")
                    elif rsi < 40:
                        score -= 10
                        drivers.append(f"{sym}: RSI weak ({rsi:.0f})")

                if not any(math.isnan(v) for v in [macd, macd_sig]):
                    if macd > macd_sig:
                        score += 5
                    else:
                        score -= 5

            except Exception as exc:
                logger.warning(f"Regime error for {sym}: {exc}")

        score = max(-100, min(100, score))
        if score >= 50:
            label = "Bull"
        elif score >= 15:
            label = "Mildly Bullish"
        elif score > -15:
            label = "Neutral / Choppy"
        elif score > -50:
            label = "Mildly Bearish"
        else:
            label = "Bear"

        return MarketRegime(label=label, score=score, drivers=drivers)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _score_symbol(
        self, symbol: str, df: pd.DataFrame
    ) -> Optional[TradeSignal]:
        """
        Score a single symbol and return a TradeSignal or None.
        """
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else row

        close = row.get("Close", float("nan"))
        if math.isnan(close) or close <= 0:
            return None

        rsi = row.get("RSI_14", float("nan"))
        atr = row.get("ATR_14", float("nan"))
        sma20 = row.get("SMA_20", float("nan"))
        sma50 = row.get("SMA_50", float("nan"))
        sma200 = row.get("SMA_200", float("nan"))
        macd = row.get("MACD", float("nan"))
        macd_sig = row.get("MACD_Signal", float("nan"))
        macd_hist = row.get("MACD_Hist", float("nan"))
        prev_macd_hist = prev.get("MACD_Hist", float("nan"))
        vol_ratio = row.get("Volume_Ratio", float("nan"))
        bb_pos = row.get("BB_Position", float("nan"))  # 0=lower band, 100=upper band

        # Guard: need at least RSI + ATR
        if math.isnan(rsi) or math.isnan(atr) or atr <= 0:
            return None

        # ---- Condition scoring ----
        long_score: float = 0.0
        short_score: float = 0.0
        patterns: List[str] = []

        # --- RSI ---
        if 50 <= rsi <= 68:
            long_score += 20
        elif 32 <= rsi <= 50:
            short_score += 20
        elif rsi < 32:
            long_score += 10          # oversold bounce potential
            patterns.append("RSI Oversold – mean reversion potential")
        elif rsi > 68:
            short_score += 10         # overbought fade potential
            patterns.append("RSI Overbought – fade / exhaustion watch")

        # --- MACD ---
        macd_bullish = False
        if not (math.isnan(macd) or math.isnan(macd_sig)):
            macd_bullish = macd > macd_sig
            if macd_bullish:
                long_score += 15
                if not math.isnan(prev_macd_hist) and prev_macd_hist < 0 <= macd_hist:
                    long_score += 10
                    patterns.append("MACD Bullish Crossover")
            else:
                short_score += 15
                if not math.isnan(prev_macd_hist) and prev_macd_hist > 0 >= macd_hist:
                    short_score += 10
                    patterns.append("MACD Bearish Crossover")

        # --- Moving average alignment ---
        ma_aligned_bull = False
        ma_aligned_bear = False
        if not any(math.isnan(v) for v in [sma20, sma50, sma200]):
            if close > sma20 > sma50 > sma200:
                ma_aligned_bull = True
                long_score += 25
                patterns.append("Bullish MA Stack (20 > 50 > 200)")
            elif close < sma20 < sma50 < sma200:
                ma_aligned_bear = True
                short_score += 25
                patterns.append("Bearish MA Stack (20 < 50 < 200)")
            elif close > sma50:
                long_score += 10
            else:
                short_score += 10

        # --- Price vs individual MAs ---
        if not math.isnan(sma20):
            if close > sma20:
                long_score += 5
            else:
                short_score += 5

        # --- Bollinger Band position ---
        if not math.isnan(bb_pos):
            if bb_pos < 20:
                long_score += 10
                patterns.append("Near Bollinger Lower Band – support zone")
            elif bb_pos > 80:
                short_score += 10
                patterns.append("Near Bollinger Upper Band – resistance zone")
            elif 40 <= bb_pos <= 60:
                long_score += 5    # mid-band, slight bullish bias

        # --- Volume confirmation ---
        vol_ok = not math.isnan(vol_ratio) and vol_ratio >= 1.2
        if vol_ok:
            long_score += 10
            short_score += 10
            patterns.append(f"Volume surge ({vol_ratio:.1f}× average)")

        # --- Determine direction ---
        total = long_score + short_score
        if total < 20:
            return None   # not enough signal

        is_long = long_score >= short_score
        direction = "LONG" if is_long else "SHORT"

        raw_conf = long_score / (long_score + short_score) * 100 if is_long else short_score / (long_score + short_score) * 100
        confidence = round(min(98, raw_conf), 1)

        if confidence < 55:
            return None   # too weak to surface

        # ---- Levels ----
        if is_long:
            stop_loss = round(close - ATR_STOP_MULT * atr, 2)
            take_profit = round(close + ATR_TARGET_MULT * atr, 2)
        else:
            stop_loss = round(close + ATR_STOP_MULT * atr, 2)
            take_profit = round(close - ATR_TARGET_MULT * atr, 2)

        risk = abs(close - stop_loss)
        reward = abs(take_profit - close)
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        # ---- Narrative ----
        narrative = _build_narrative(
            symbol=symbol,
            direction=direction,
            rsi=rsi,
            macd_bullish=macd_bullish,
            ma_aligned=ma_aligned_bull if is_long else ma_aligned_bear,
            vol_ratio=vol_ratio if not math.isnan(vol_ratio) else 1.0,
            bb_pos=bb_pos if not math.isnan(bb_pos) else 50.0,
            confidence=confidence,
        )

        return TradeSignal(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            entry=round(close, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=rr,
            rsi=round(rsi, 1),
            atr=round(atr, 2),
            volume_ratio=round(vol_ratio, 2) if not math.isnan(vol_ratio) else 1.0,
            macd_bullish=macd_bullish,
            ma_aligned=ma_aligned_bull if is_long else ma_aligned_bear,
            narrative=narrative,
            patterns=patterns,
            sector=SECTOR_MAP.get(symbol, "Unknown"),
        )


# ---------------------------------------------------------------------------
# Narrative builder
# ---------------------------------------------------------------------------

def _build_narrative(
    symbol: str,
    direction: str,
    rsi: float,
    macd_bullish: bool,
    ma_aligned: bool,
    vol_ratio: float,
    bb_pos: float,
    confidence: float,
) -> str:
    """
    Construct a human-readable trade narrative from indicator conditions.
    """
    lines: List[str] = []

    arrow = "📈" if direction == "LONG" else "📉"
    strength = "high-confidence" if confidence >= 80 else "moderate-confidence"
    bias = "bullish" if direction == "LONG" else "bearish"

    lines.append(
        f"{arrow} **{symbol}** is showing a {strength} **{direction}** setup "
        f"with a {bias} market structure."
    )

    # RSI comment
    if rsi > 65:
        lines.append(
            f"RSI is elevated at **{rsi:.0f}**, indicating strong momentum that "
            f"may continue if volume holds."
        )
    elif 50 < rsi <= 65:
        lines.append(
            f"RSI at **{rsi:.0f}** sits in the healthy momentum zone (50–65), "
            f"suggesting room to run without being overbought."
        )
    elif 35 <= rsi <= 50:
        lines.append(
            f"RSI at **{rsi:.0f}** reflects weakening momentum; sellers are "
            f"in control on the shorter timeframe."
        )
    else:
        lines.append(
            f"RSI at **{rsi:.0f}** signals oversold / overbought extreme — "
            f"watch for a potential mean reversion move."
        )

    # MACD comment
    if macd_bullish:
        lines.append(
            "The MACD line is **above its signal line**, confirming positive "
            "momentum divergence."
        )
    else:
        lines.append(
            "The MACD line is **below its signal line**, underscoring "
            "bearish price pressure."
        )

    # MA alignment
    if ma_aligned:
        align_word = "bullish" if direction == "LONG" else "bearish"
        lines.append(
            f"Moving averages are in **full {align_word} alignment** "
            f"(20 > 50 > 200), providing a high-probability trend backdrop."
        )
    else:
        lines.append(
            "Moving averages are not fully aligned; treat this as a "
            "counter-trend or range-based setup."
        )

    # Volume comment
    if vol_ratio >= 2.0:
        lines.append(
            f"Volume is running at **{vol_ratio:.1f}×** the 20-day average — "
            f"strong institutional participation."
        )
    elif vol_ratio >= 1.2:
        lines.append(
            f"Volume is **{vol_ratio:.1f}× average**, providing moderate "
            f"confirmation of the price move."
        )

    # Bollinger comment
    if bb_pos < 25 and direction == "LONG":
        lines.append(
            "Price is trading **near the lower Bollinger Band**, a historically "
            "attractive entry zone for long setups."
        )
    elif bb_pos > 75 and direction == "SHORT":
        lines.append(
            "Price is trading **near the upper Bollinger Band**, a "
            "stretched level that often precedes short-term reversals."
        )

    # Risk note
    lines.append(
        f"\n⚠️ *Always confirm with your risk parameters. Confidence score: {confidence:.0f}/100.*"
    )

    return "  \n".join(lines)


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=900, show_spinner=False)
def _load_bulk_data(symbols: Tuple[str, ...], period: str) -> Dict[str, pd.DataFrame]:
    """Cached bulk historical download (uppercase OHLCV, date as index)."""
    yf = YahooFinanceConnector()
    return yf.fetch_bulk_historical(list(symbols), period=period)


@st.cache_data(ttl=900, show_spinner=False)
def _load_single(symbol: str, period: str) -> Optional[pd.DataFrame]:
    """Cached single-symbol fetch."""
    yf = YahooFinanceConnector()
    return yf.fetch_historical_data(symbol, period=period)


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------

def _mini_chart(symbol: str, df: pd.DataFrame) -> go.Figure:
    """Compact candlestick + SMA20 chart."""
    enriched = TechnicalIndicators().add_moving_averages(df, sma_periods=[20])
    recent = enriched.tail(60)

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=recent.index,
            open=recent["Open"],
            high=recent["High"],
            low=recent["Low"],
            close=recent["Close"],
            name=symbol,
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        )
    )
    if "SMA_20" in recent.columns:
        fig.add_trace(
            go.Scatter(
                x=recent.index,
                y=recent["SMA_20"],
                name="SMA 20",
                line=dict(color="orange", width=1.5),
            )
        )
    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=24, b=0),
        xaxis_rangeslider_visible=False,
        showlegend=False,
        title=dict(text=symbol, font=dict(size=13)),
    )
    return fig


# ---------------------------------------------------------------------------
# Sub-renderers
# ---------------------------------------------------------------------------

def _render_trade_signals(
    signals: List[TradeSignal], raw_data: Dict[str, pd.DataFrame]
) -> None:
    """Render the Trade Signals tab."""
    st.subheader("🎯 AI Trade Signals")

    if not signals:
        st.info(
            "No strong signals found for the current watchlist. "
            "Try refreshing or expanding the watchlist."
        )
        return

    # Summary metrics
    longs = [s for s in signals if s.direction == "LONG"]
    shorts = [s for s in signals if s.direction == "SHORT"]
    avg_conf = sum(s.confidence for s in signals) / len(signals)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Signals", len(signals))
    m2.metric("🟢 Long", len(longs))
    m3.metric("🔴 Short", len(shorts))
    m4.metric("Avg Confidence", f"{avg_conf:.1f}%")

    st.markdown("---")

    # Signal table
    table_rows = []
    for s in signals:
        dir_badge = "🟢 LONG" if s.direction == "LONG" else "🔴 SHORT"
        table_rows.append(
            {
                "Symbol": s.symbol,
                "Sector": s.sector,
                "Direction": dir_badge,
                "Entry $": f"{s.entry:.2f}",
                "Stop Loss $": f"{s.stop_loss:.2f}",
                "Take Profit $": f"{s.take_profit:.2f}",
                "R/R": f"{s.risk_reward:.1f}×",
                "Confidence": f"{s.confidence:.0f}%",
                "RSI": f"{s.rsi:.0f}",
                "Vol Ratio": f"{s.volume_ratio:.1f}×",
            }
        )

    df_table = pd.DataFrame(table_rows)
    st.dataframe(df_table, use_container_width=True, hide_index=True)

    st.markdown("---")

    # Detailed signal cards
    st.subheader("📋 Detailed Signal Cards")

    cols_per_row = 2
    for i in range(0, len(signals), cols_per_row):
        row_sigs = signals[i : i + cols_per_row]
        cols = st.columns(cols_per_row)

        for col, sig in zip(cols, row_sigs):
            with col:
                border_color = "#26a69a" if sig.direction == "LONG" else "#ef5350"
                dir_icon = "🟢" if sig.direction == "LONG" else "🔴"

                st.markdown(
                    f"""
<div style="border:1px solid {border_color};border-radius:8px;padding:14px;margin-bottom:10px;">
<h4 style="margin:0 0 8px 0;">{dir_icon} {sig.symbol} — <span style="color:{border_color};">{sig.direction}</span></h4>
<table style="width:100%;font-size:0.88rem;">
  <tr><td><b>Entry</b></td><td>${sig.entry:.2f}</td><td><b>Stop Loss</b></td><td style="color:#ef5350;">${sig.stop_loss:.2f}</td></tr>
  <tr><td><b>Take Profit</b></td><td style="color:#26a69a;">${sig.take_profit:.2f}</td><td><b>R/R</b></td><td>{sig.risk_reward:.1f}×</td></tr>
  <tr><td><b>Confidence</b></td><td>{sig.confidence:.0f}%</td><td><b>RSI</b></td><td>{sig.rsi:.0f}</td></tr>
  <tr><td><b>ATR</b></td><td>${sig.atr:.2f}</td><td><b>Vol</b></td><td>{sig.volume_ratio:.1f}×</td></tr>
</table>
</div>""",
                    unsafe_allow_html=True,
                )

                # Expander with narrative and chart
                with st.expander(f"AI Narrative & Chart — {sig.symbol}"):
                    st.markdown(sig.narrative)

                    if sig.patterns:
                        st.markdown("**📐 Patterns detected:**")
                        for p in sig.patterns:
                            st.markdown(f"- {p}")

                    df_sym = raw_data.get(sig.symbol)
                    if df_sym is not None and not df_sym.empty and len(df_sym) >= 20:
                        try:
                            st.plotly_chart(
                                _mini_chart(sig.symbol, df_sym),
                                use_container_width=True,
                            )
                        except Exception:
                            pass


def _render_market_commentary(
    regime: MarketRegime, index_data: Dict[str, pd.DataFrame]
) -> None:
    """Render the Market Commentary tab."""
    st.subheader("🌍 Market Commentary")

    # Regime badge
    regime_colors = {
        "Bull": "#26a69a",
        "Mildly Bullish": "#66bb6a",
        "Neutral / Choppy": "#ffa726",
        "Mildly Bearish": "#ef9a9a",
        "Bear": "#ef5350",
    }
    color = regime_colors.get(regime.label, "#888")

    st.markdown(
        f"""
<div style="background:{color}22;border:2px solid {color};border-radius:8px;
            padding:16px;text-align:center;margin-bottom:16px;">
<h3 style="margin:0;color:{color};">Market Regime: {regime.label}</h3>
<p style="margin:4px 0 0 0;">Composite score: <b>{regime.score}</b> / 100</p>
</div>""",
        unsafe_allow_html=True,
    )

    if regime.drivers:
        st.markdown("**Key regime drivers:**")
        for d in regime.drivers:
            st.markdown(f"- {d}")

    # Regime interpretation
    if regime.score >= 50:
        st.success(
            "The broad market is in a **strong uptrend**. "
            "Favour long setups; momentum strategies are well-suited for this environment. "
            "Be cautious about over-extending positions at highs."
        )
    elif regime.score >= 15:
        st.info(
            "The market shows a **mild bullish bias**. "
            "Longs are favoured but selectivity is key. "
            "Keep stops tight in case the rally stalls."
        )
    elif regime.score > -15:
        st.warning(
            "The market is **neutral / choppy**. "
            "Both longs and shorts carry elevated stop-out risk. "
            "Consider reducing position size or waiting for a clearer trend."
        )
    elif regime.score > -50:
        st.warning(
            "The market has a **mild bearish tone**. "
            "Short setups may offer better risk/reward. "
            "Protect long positions with tighter stops."
        )
    else:
        st.error(
            "The market is in a **bear trend**. "
            "Capital preservation is the priority. "
            "Favour defensive sectors, shorts, or cash."
        )

    st.markdown("---")

    # Index breakdown
    st.subheader("📊 Index Breakdown")
    index_labels = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000"}

    idx_cols = st.columns(len(index_labels))
    ind_client = TechnicalIndicators()

    for col, (sym, label) in zip(idx_cols, index_labels.items()):
        df = index_data.get(sym)
        with col:
            if df is None or df.empty or len(df) < 5:
                st.metric(label, "N/A")
                continue

            try:
                enriched = ind_client.add_all_indicators(df)
                last = enriched.iloc[-1]
                prev = enriched.iloc[-2]

                close = last["Close"]
                change = close - prev["Close"]
                change_pct = change / prev["Close"] * 100
                rsi = last.get("RSI_14", float("nan"))

                st.metric(
                    label,
                    f"${close:.2f}",
                    f"{change:+.2f} ({change_pct:+.2f}%)",
                )

                if not math.isnan(rsi):
                    st.caption(f"RSI: {rsi:.1f}")

            except Exception as exc:
                logger.warning(f"Index metric error {sym}: {exc}")
                st.metric(label, "Error")

    # 30-day SPY chart
    st.markdown("---")
    spy_df = index_data.get("SPY")
    if spy_df is not None and not spy_df.empty and len(spy_df) >= 20:
        st.subheader("SPY — 30-Day Price Chart")
        try:
            st.plotly_chart(
                _mini_chart("SPY", spy_df.tail(60)), use_container_width=True
            )
        except Exception:
            pass

    # Narrative
    st.markdown("---")
    st.subheader("📝 AI Market Commentary")
    commentary = _generate_market_commentary(regime)
    st.markdown(commentary)


def _generate_market_commentary(regime: MarketRegime) -> str:
    """
    Generate a natural-language market commentary paragraph from the regime.
    """
    now = datetime.now().strftime("%B %d, %Y")
    base = (
        f"As of **{now}**, the algorithmic regime model scores the broad equity "
        f"market at **{regime.score} / 100**, indicating a **{regime.label}** "
        f"environment. "
    )

    if regime.score >= 50:
        body = (
            "All three major indices (SPY, QQQ, IWM) are trading above their key "
            "moving averages with MACD confirming positive momentum. "
            "Momentum and trend-following strategies are historically most effective "
            "in this type of environment. "
            "Traders should look for pullbacks to the 20-day SMA as long entries, "
            "and leaning against obvious resistance levels for partial profit-taking."
        )
    elif regime.score >= 15:
        body = (
            "The primary trend remains upward but breadth is narrowing. "
            "Large-cap tech and defensive growth names are leading, while small-caps lag. "
            "In this environment selectivity matters more than simply being long: "
            "favour high-quality, high-confidence setups with defined risk."
        )
    elif regime.score > -15:
        body = (
            "Price action is range-bound and indices are oscillating around their "
            "50-day moving averages without conviction. "
            "Breakout strategies are experiencing elevated failure rates. "
            "Traders should reduce leverage, widen stops, or wait for the market "
            "to pick a direction before committing to new positions."
        )
    elif regime.score > -50:
        body = (
            "Distribution is evident in the tape: lower highs, declining breadth, "
            "and MACD rolling over below the signal line. "
            "Defensive positioning is recommended. "
            "Short setups with defined risk on individual names showing relative "
            "weakness are opportunistic."
        )
    else:
        body = (
            "The market is in a confirmed downtrend: all major indices are below "
            "their 50- and 200-day moving averages with heavy selling volume. "
            "Capital preservation is paramount. "
            "Avoid catching falling knives; cash, inverse ETFs, or "
            "carefully sized short positions are the primary tools."
        )

    tail = (
        "\n\n*This commentary is generated by a rule-based technical analysis engine "
        "and is for educational purposes only. It does not constitute financial advice.*"
    )

    return base + body + tail


def _render_pattern_insights(signals: List[TradeSignal]) -> None:
    """Render the Pattern Recognition tab."""
    st.subheader("🔍 Pattern Recognition Insights")

    if not signals:
        st.info("Run a scan first to populate pattern insights.")
        return

    # Aggregate all patterns
    pattern_map: Dict[str, List[str]] = {}
    for sig in signals:
        for pat in sig.patterns:
            pattern_map.setdefault(pat, []).append(sig.symbol)

    if not pattern_map:
        st.info("No specific patterns detected in the current scan results.")
        return

    # Pattern frequency bar chart
    pat_names = list(pattern_map.keys())
    pat_counts = [len(pattern_map[p]) for p in pat_names]

    fig = go.Figure(
        go.Bar(
            x=pat_counts,
            y=pat_names,
            orientation="h",
            marker_color="#1f77b4",
        )
    )
    fig.update_layout(
        height=max(200, 40 * len(pat_names)),
        margin=dict(l=0, r=0, t=24, b=0),
        xaxis_title="Number of stocks",
        title="Pattern Frequency Across Watchlist",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # Pattern detail cards
    for pat, symbols in sorted(pattern_map.items(), key=lambda x: -len(x[1])):
        with st.expander(f"**{pat}** — {len(symbols)} stock(s)"):
            st.write(", ".join(f"`{s}`" for s in symbols))
            st.markdown(_pattern_explanation(pat))


def _pattern_explanation(pattern: str) -> str:
    """Return a brief explanation of a detected pattern."""
    explanations: Dict[str, str] = {
        "Bullish MA Stack (20 > 50 > 200)": (
            "All three major moving averages are in upward sequence and price is "
            "above them. This is the strongest trend confirmation in classical "
            "technical analysis."
        ),
        "Bearish MA Stack (20 < 50 < 200)": (
            "Moving averages are in downward sequence and price is below them — "
            "the classic bear-trend confirmation. Short sellers favour entering "
            "on rallies to the 20 SMA."
        ),
        "MACD Bullish Crossover": (
            "The MACD line has just crossed above the signal line from below. "
            "This is one of the most widely used momentum buy signals."
        ),
        "MACD Bearish Crossover": (
            "The MACD line has crossed below the signal line, signalling a shift "
            "from positive to negative momentum — a common short entry trigger."
        ),
        "RSI Oversold – mean reversion potential": (
            "RSI < 32 suggests the stock may be approaching a short-term low. "
            "Mean-reversion traders often look to buy at these extremes with a "
            "tight stop below recent lows."
        ),
        "RSI Overbought – fade / exhaustion watch": (
            "RSI > 68 indicates aggressive buying; many traders watch for signs "
            "of momentum stalling to fade the move or tighten trailing stops."
        ),
        "Near Bollinger Lower Band – support zone": (
            "Price is pressing against the lower band, a zone that statistically "
            "acts as dynamic support. Countertrend long setups are common here."
        ),
        "Near Bollinger Upper Band – resistance zone": (
            "Price is extended to the upper band. In trending markets this can "
            "continue, but in choppy markets it often marks a short-term top."
        ),
    }
    return explanations.get(
        pattern,
        "A technical pattern was detected. Review the chart for further context.",
    )


def _render_risk_warnings(
    signals: List[TradeSignal], regime: Optional[MarketRegime]
) -> None:
    """Render the Risk Warnings tab."""
    st.subheader("⚠️ Risk Warnings")

    warnings: List[Tuple[str, str, str]] = []  # (level, title, body)

    # Market-level warnings
    if regime is not None:
        if regime.score <= -50:
            warnings.append(
                (
                    "error",
                    "Bear Market Detected",
                    "The composite regime score is deeply negative. "
                    "Long positions carry significantly elevated drawdown risk. "
                    "Consider cash or hedging strategies.",
                )
            )
        elif regime.score <= -15:
            warnings.append(
                (
                    "warning",
                    "Bearish Regime",
                    "Market momentum is negative. "
                    "Long setups may underperform. Raise cash levels.",
                )
            )
        elif regime.score >= 80:
            warnings.append(
                (
                    "warning",
                    "Overbought Market",
                    "The broad market is running hot. "
                    "Guard against chasing breakouts at extended levels. "
                    "Trailing stops are recommended.",
                )
            )

    # Signal-level warnings
    high_rsi_shorts = [s for s in signals if s.direction == "SHORT" and s.rsi > 65]
    low_rsi_longs = [s for s in signals if s.direction == "LONG" and s.rsi > 68]

    if low_rsi_longs:
        syms = ", ".join(s.symbol for s in low_rsi_longs)
        warnings.append(
            (
                "warning",
                f"Overbought Long Signals ({len(low_rsi_longs)})",
                f"The following long signals have RSI > 68 — chasing into "
                f"overbought conditions carries higher reversal risk: **{syms}**.",
            )
        )

    low_rr = [s for s in signals if s.risk_reward < 1.5]
    if low_rr:
        syms = ", ".join(s.symbol for s in low_rr)
        warnings.append(
            (
                "warning",
                f"Poor Risk/Reward Ratio ({len(low_rr)} signals)",
                f"These signals have R/R < 1.5×, below the recommended "
                f"minimum of 2×: **{syms}**. "
                f"Consider skipping or adjusting targets.",
            )
        )

    # Concentration warning
    sector_counts: Dict[str, int] = {}
    for s in signals:
        sector_counts[s.sector] = sector_counts.get(s.sector, 0) + 1

    dominant_sector = max(sector_counts, key=sector_counts.get) if sector_counts else None
    if dominant_sector and sector_counts[dominant_sector] >= 3:
        warnings.append(
            (
                "warning",
                f"Sector Concentration — {dominant_sector}",
                f"{sector_counts[dominant_sector]} of the current signals are "
                f"in {dominant_sector}. "
                f"Over-exposure to a single sector magnifies idiosyncratic risk.",
            )
        )

    # General best-practice reminders
    warnings.append(
        (
            "info",
            "Position Sizing Reminder",
            "Never risk more than 1–2% of your account on a single trade. "
            "Use the ATR-based stop levels provided to calculate your position size.",
        )
    )
    warnings.append(
        (
            "info",
            "Not Financial Advice",
            "These signals are generated by an automated technical-analysis "
            "engine. Always conduct independent research before trading.",
        )
    )

    if not warnings:
        st.success("✅ No critical risk warnings at this time.")
        return

    for level, title, body in warnings:
        if level == "error":
            st.error(f"**{title}**  \n{body}")
        elif level == "warning":
            st.warning(f"**{title}**  \n{body}")
        else:
            st.info(f"**{title}**  \n{body}")


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render() -> None:
    """Render the AI Insights page."""
    st.title("🤖 AI Insights")
    st.caption(
        "Rule-based signal engine powered by technical analysis — "
        "Long / Short calls with entry, stop-loss, take-profit, and AI narratives."
    )

    # ---- Sidebar controls ----
    with st.sidebar:
        st.markdown("---")
        st.subheader("🤖 AI Insights Settings")

        period_map = {
            "3 Months": "3mo",
            "6 Months": "6mo",
            "1 Year": "1y",
        }
        period_label = st.selectbox(
            "Historical data period",
            list(period_map.keys()),
            index=1,
            key="ai_period",
        )
        period = period_map[period_label]

        preset_map = {
            "Large-Cap Tech + ETFs": [
                "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
                "AMD", "ADBE", "CRM", "SPY", "QQQ", "IWM",
            ],
            "Full Watchlist (30+)": DEFAULT_WATCHLIST,
            "Custom": None,
        }
        preset = st.selectbox(
            "Watchlist preset",
            list(preset_map.keys()),
            key="ai_preset",
        )

        if preset == "Custom":
            custom_raw = st.text_area(
                "Enter tickers (comma-separated)",
                value="AAPL, MSFT, NVDA, TSLA",
                key="ai_custom_tickers",
            )
            watchlist = [t.strip().upper() for t in custom_raw.split(",") if t.strip()]
        else:
            watchlist = preset_map[preset]  # type: ignore[assignment]

        min_confidence = st.slider(
            "Min confidence threshold (%)", 55, 90, 65, key="ai_min_conf"
        )

        run_scan = st.button("🚀 Run AI Scan", type="primary", key="ai_run")

    # ---- Tabs ----
    tab_signals, tab_market, tab_patterns, tab_risk = st.tabs(
        ["🎯 Trade Signals", "🌍 Market Commentary", "🔍 Patterns", "⚠️ Risk Warnings"]
    )

    # ---- State ----
    if "ai_signals" not in st.session_state:
        st.session_state.ai_signals = []
    if "ai_regime" not in st.session_state:
        st.session_state.ai_regime = None
    if "ai_raw_data" not in st.session_state:
        st.session_state.ai_raw_data = {}
    if "ai_index_data" not in st.session_state:
        st.session_state.ai_index_data = {}
    if "ai_last_scan" not in st.session_state:
        st.session_state.ai_last_scan = None

    # ---- Run scan ----
    if run_scan:
        engine = AISignalEngine()
        index_syms = ["SPY", "QQQ", "IWM"]
        all_syms = list({*watchlist, *index_syms})  # dedup

        with st.spinner(f"Downloading data for {len(all_syms)} symbols…"):
            try:
                raw = _load_bulk_data(tuple(sorted(all_syms)), period)
            except Exception as exc:
                st.error(f"Data download failed: {exc}")
                raw = {}

        index_data = {s: raw[s] for s in index_syms if s in raw}
        signal_data = {s: raw[s] for s in watchlist if s in raw}

        with st.spinner("Running AI signal engine…"):
            all_signals = engine.generate_signals(signal_data)
            regime = engine.analyse_market_regime(index_data)

        # Filter by confidence threshold
        filtered = [s for s in all_signals if s.confidence >= min_confidence]

        st.session_state.ai_signals = filtered
        st.session_state.ai_regime = regime
        st.session_state.ai_raw_data = raw
        st.session_state.ai_index_data = index_data
        st.session_state.ai_last_scan = datetime.now().strftime("%H:%M:%S")

        # Invalidate cache so next scan fetches fresh data
        _load_bulk_data.clear()
        _load_single.clear()

        st.success(
            f"✅ Scan complete — {len(filtered)} signal(s) found "
            f"(confidence ≥ {min_confidence}%)"
        )

    # ---- Last scan info ----
    if st.session_state.ai_last_scan:
        st.caption(f"Last scan: {st.session_state.ai_last_scan}")
    else:
        st.info(
            "👆 Configure your watchlist and click **Run AI Scan** to generate signals."
        )

    # ---- Render tabs ----
    with tab_signals:
        _render_trade_signals(
            st.session_state.ai_signals,
            st.session_state.ai_raw_data,
        )

    with tab_market:
        _render_market_commentary(
            st.session_state.ai_regime or MarketRegime("Neutral / Choppy", 0),
            st.session_state.ai_index_data,
        )

    with tab_patterns:
        _render_pattern_insights(st.session_state.ai_signals)

    with tab_risk:
        _render_risk_warnings(
            st.session_state.ai_signals,
            st.session_state.ai_regime,
        )
