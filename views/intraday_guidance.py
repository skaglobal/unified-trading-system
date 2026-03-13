"""
Intraday Trading Guidance — Streamlit Page
==========================================
Real-time intraday decision-support panel powered by IBKR live data.

Layout
------
┌─────────────────────────────────────────────────────┐
│  Ticker input  •  Connection status  •  Refresh btn  │
├──────────────────────┬──────────────────────────────┤
│  Score panel         │  Candlestick chart (Plotly)  │
│  Long / Short prob   │  VWAP • SMA20/50/200         │
│  Confidence label    │  S/R lines                   │
│  Top reasons         │  Volume sub-panel            │
├──────────────────────┴──────────────────────────────┤
│  Guidance panel (entry / stop / targets / R-R)      │
├─────────────────────────────────────────────────────┤
│  Indicator metrics row                              │
├─────────────────────────────────────────────────────┤
│  Alerts feed                                        │
├─────────────────────────────────────────────────────┤
│  Paper-trade log / Backtest panel                   │
└─────────────────────────────────────────────────────┘

DISCLAIMER: All output is probability guidance, not guaranteed prediction.
            This tool is for educational decision support only —
            not for automated execution.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from intraday.alert_engine import Alert, AlertEngine
from intraday.backtest_mode import IntradayBacktester
from intraday.guidance_engine import GuidanceEngine, GuidanceResult
from intraday.indicator_engine import IndicatorEngine, IndicatorSnapshot
from intraday.market_data_provider import IBKRMarketDataProvider, QuoteData
from intraday.paper_trade_logger import PaperTradeLogger
from intraday.scoring_engine import ScoreResult, ScoringEngine

logger = logging.getLogger("intraday.dashboard")

# ── Session-state keys ────────────────────────────────────────────────────────
_KEY_TICKER      = "igt_ticker"
_KEY_PREV_SCORE  = "igt_prev_score"
_KEY_ALERT_ENG   = "igt_alert_engine"
_KEY_PT_LOGGER   = "igt_pt_logger"
_KEY_SNAP        = "igt_last_snap"
_KEY_SCORE       = "igt_last_score"
_KEY_GUIDANCE    = "igt_last_guidance"
_KEY_ALERTS      = "igt_alerts"
_KEY_INTRADAY_DF = "igt_intraday_df"
_KEY_DAILY_DF    = "igt_daily_df"
_KEY_QUOTE       = "igt_last_quote"
_KEY_REFRESH_TS  = "igt_refresh_ts"


# ---------------------------------------------------------------------------
# Main render function (called from streamlit_app.py)
# ---------------------------------------------------------------------------

def render() -> None:
    """Render the full intraday guidance page."""

    st.title("⚡ Intraday Trading Guidance")
    st.caption(
        "📘 **Educational tool — probability guidance only, not guaranteed prediction.**  "
        "Connect IBKR Gateway / TWS before using."
    )

    # ── Init session state ────────────────────────────────────────────────
    _init_session_state()

    # ── Header controls ───────────────────────────────────────────────────
    ibkr = st.session_state.get("ibkr")
    _render_controls(ibkr)

    ticker = st.session_state.get(_KEY_TICKER, "").upper().strip()
    if not ticker:
        st.info("Enter a ticker symbol above to start.")
        return

    if ibkr is None or not ibkr.is_connected():
        st.warning("⚠️  IBKR is not connected. Connect via the Configuration page or sidebar.")
        _render_demo_mode(ticker)
        return

    # ── Engine singletons (cached in session state) ───────────────────────
    provider      = IBKRMarketDataProvider(ibkr)
    ind_engine    = IndicatorEngine()
    score_engine  = ScoringEngine()
    guide_engine  = GuidanceEngine()
    alert_engine: AlertEngine   = st.session_state[_KEY_ALERT_ENG]
    pt_logger: PaperTradeLogger = st.session_state[_KEY_PT_LOGGER]

    # ── Auto-refresh trigger ──────────────────────────────────────────────
    poll_secs = 5
    last_refresh = st.session_state.get(_KEY_REFRESH_TS, 0)
    time_since   = time.time() - last_refresh

    manual_refresh = st.session_state.pop("_igt_manual_refresh", False)
    auto_refresh   = time_since >= poll_secs

    if manual_refresh or auto_refresh or st.session_state.get(_KEY_SNAP) is None:
        with st.spinner(f"📡 Fetching live data for **{ticker}** …"):
            _fetch_and_compute(
                ticker=ticker,
                provider=provider,
                ind_engine=ind_engine,
                score_engine=score_engine,
                guide_engine=guide_engine,
                alert_engine=alert_engine,
                pt_logger=pt_logger,
            )
        st.session_state[_KEY_REFRESH_TS] = time.time()

    # ── Render panels ─────────────────────────────────────────────────────
    snap: Optional[IndicatorSnapshot] = st.session_state.get(_KEY_SNAP)
    score: Optional[ScoreResult]      = st.session_state.get(_KEY_SCORE)
    guidance: Optional[GuidanceResult]= st.session_state.get(_KEY_GUIDANCE)
    quote: Optional[QuoteData]        = st.session_state.get(_KEY_QUOTE)

    if snap is None or score is None:
        st.warning("No data yet — check connection or try a different ticker.")
        return

    # Score + chart side by side
    col_score, col_chart = st.columns([1, 3])
    with col_score:
        _render_score_panel(score, snap, quote)
    with col_chart:
        _render_chart(ticker, snap, score)

    st.markdown("---")
    _render_guidance_panel(guidance, score, snap)

    st.markdown("---")
    _render_indicators_row(snap, quote)

    st.markdown("---")
    _render_alerts_feed()

    st.markdown("---")
    _render_paper_trade_section(pt_logger, ticker, ibkr, provider)

    # ── Schedule next auto-refresh ────────────────────────────────────────
    time.sleep(0.05)
    st.rerun()


# ---------------------------------------------------------------------------
# Controls (ticker input + status)
# ---------------------------------------------------------------------------

def _render_controls(ibkr) -> None:
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        ticker_input = st.text_input(
            "🔎 Ticker symbol",
            value=st.session_state.get(_KEY_TICKER, ""),
            placeholder="e.g. AAPL, TSLA, SPY",
            key="_igt_ticker_input",
        )
        if ticker_input.upper().strip() != st.session_state.get(_KEY_TICKER, ""):
            st.session_state[_KEY_TICKER] = ticker_input.upper().strip()
            # Reset all cached data on ticker change
            for k in [_KEY_SNAP, _KEY_SCORE, _KEY_GUIDANCE, _KEY_QUOTE,
                      _KEY_INTRADAY_DF, _KEY_DAILY_DF, _KEY_PREV_SCORE]:
                st.session_state.pop(k, None)
            st.session_state[_KEY_ALERTS] = []
            st.session_state[_KEY_ALERT_ENG] = AlertEngine()
            st.rerun()

    with col2:
        if ibkr and ibkr.is_connected():
            st.success("🟢 IBKR Connected")
        else:
            st.error("🔴 IBKR Disconnected")

    with col3:
        if st.button("🔄 Refresh Now", key="_igt_refresh_btn"):
            st.session_state["_igt_manual_refresh"] = True
            st.rerun()


# ---------------------------------------------------------------------------
# Data fetch + compute cycle
# ---------------------------------------------------------------------------

def _fetch_and_compute(
    ticker: str,
    provider: IBKRMarketDataProvider,
    ind_engine: IndicatorEngine,
    score_engine: ScoringEngine,
    guide_engine: GuidanceEngine,
    alert_engine: AlertEngine,
    pt_logger: PaperTradeLogger,
) -> None:
    """Fetch live data, run all engines, store results in session state."""
    try:
        # ── Live quote ────────────────────────────────────────────────────
        quote = provider.get_latest_quote(ticker)
        st.session_state[_KEY_QUOTE] = quote

        # ── Intraday bars (cache and extend on each refresh) ──────────────
        df_intraday: Optional[pd.DataFrame] = st.session_state.get(_KEY_INTRADAY_DF)
        if df_intraday is None or df_intraday.empty:
            df_intraday = provider.get_intraday_candles(
                symbol=ticker, bar_size="1 min", duration="1 D", use_rth=True
            )
            st.session_state[_KEY_INTRADAY_DF] = df_intraday

        # ── Daily bars for SMA200 (fetch once per session) ────────────────
        df_daily: Optional[pd.DataFrame] = st.session_state.get(_KEY_DAILY_DF)
        if df_daily is None:
            df_daily = provider.get_daily_candles(symbol=ticker, duration="250 D")
            st.session_state[_KEY_DAILY_DF] = df_daily

        if df_intraday is None or df_intraday.empty:
            logger.warning("No intraday bars for %s", ticker)
            return

        # ── Compute indicators ────────────────────────────────────────────
        snap = ind_engine.compute(
            intraday_df=df_intraday,
            quote=quote,
            daily_df=df_daily,
        )
        st.session_state[_KEY_SNAP] = snap

        # ── Score ─────────────────────────────────────────────────────────
        prev_score: Optional[ScoreResult] = st.session_state.get(_KEY_PREV_SCORE)
        score = score_engine.score(snap)
        st.session_state[_KEY_SCORE]      = score
        st.session_state[_KEY_PREV_SCORE] = score

        # ── Guidance ──────────────────────────────────────────────────────
        guidance = guide_engine.compute_guidance(
            score=score, snap=snap, prev_score=prev_score
        )
        st.session_state[_KEY_GUIDANCE] = guidance

        # ── Alerts ────────────────────────────────────────────────────────
        new_alerts = alert_engine.check_alerts(
            score=score, snap=snap, prev_score=prev_score
        )
        existing: List[Alert] = st.session_state.get(_KEY_ALERTS, [])
        st.session_state[_KEY_ALERTS] = (existing + new_alerts)[-50:]  # keep last 50

        # ── Log signal to paper-trade log ─────────────────────────────────
        if score.dominant_side != "none" and new_alerts:
            signal_type = new_alerts[0].alert_type.value if new_alerts else score.confidence_label
            pt_logger.log_signal(score, snap, guidance, signal_type=signal_type, mode="live")

    except Exception as exc:
        logger.error("_fetch_and_compute(%s) error: %s", ticker, exc, exc_info=True)
        st.error(f"Data error: {exc}")


# ---------------------------------------------------------------------------
# Score panel
# ---------------------------------------------------------------------------

def _render_score_panel(
    score: ScoreResult, snap: IndicatorSnapshot, quote: Optional[QuoteData]
) -> None:
    label = score.confidence_label
    label_colors = {
        "Strong Long":  ("🟢🟢", "#00c853"),
        "Weak Long":    ("🟢",   "#69f0ae"),
        "No Trade":     ("⚪",   "#9e9e9e"),
        "Weak Short":   ("🔴",   "#ff6d00"),
        "Strong Short": ("🔴🔴", "#d50000"),
    }
    emoji, color = label_colors.get(label, ("⚪", "#9e9e9e"))

    st.markdown(f"""
    <div style='text-align:center; padding:12px; border-radius:10px;
                border:2px solid {color}; background:rgba(0,0,0,0.04);'>
      <div style='font-size:2rem;'>{emoji}</div>
      <div style='font-size:1.4rem; font-weight:bold; color:{color};'>{label}</div>
      <div style='font-size:0.8rem; color:#aaa;'>Confidence</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("&nbsp;")

    c1, c2 = st.columns(2)
    c1.metric("🟢 Long %", f"{score.long_score:.0f}")
    c2.metric("🔴 Short %", f"{score.short_score:.0f}")

    # Progress bars
    st.markdown("**Long probability**")
    st.progress(int(score.long_score))
    st.markdown("**Short probability**")
    st.progress(int(score.short_score))

    # Live quote strip
    if quote and quote.last:
        st.markdown("---")
        st.markdown("**Last / Bid / Ask**")
        qa, qb, qc = st.columns(3)
        qa.metric("Last",  f"${quote.last:.2f}" if quote.last else "—")
        qb.metric("Bid",   f"${quote.bid:.2f}"  if quote.bid  else "—")
        qc.metric("Ask",   f"${quote.ask:.2f}"  if quote.ask  else "—")
        if quote.bid_size or quote.ask_size:
            qd, qe = st.columns(2)
            qd.metric("Bid sz", str(quote.bid_size or "—"))
            qe.metric("Ask sz", str(quote.ask_size or "—"))
        if quote.bid_ask_imbalance is not None:
            imb = quote.bid_ask_imbalance
            imb_pct = f"{imb*100:.0f}% bid"
            st.metric("Imbalance", imb_pct, delta="" if abs(imb - 0.5) < 0.1 else ("Buy pressure" if imb > 0.5 else "Sell pressure"))

    # Top reasons
    st.markdown("---")
    if score.dominant_side == "long" and score.long_reasons:
        st.markdown("**Top bullish factors**")
        for r in score.long_reasons[:5]:
            st.markdown(f"✅ {r}")
    elif score.dominant_side == "short" and score.short_reasons:
        st.markdown("**Top bearish factors**")
        for r in score.short_reasons[:5]:
            st.markdown(f"🔻 {r}")
    else:
        st.markdown("**Neutral / mixed signals**")
        for r in (score.long_reasons[:2] + score.short_reasons[:2]):
            st.markdown(f"ℹ️  {r}")


# ---------------------------------------------------------------------------
# Main chart
# ---------------------------------------------------------------------------

# Chart theme constants — dark palette compatible with Streamlit dark mode
_CHART_PAPER_BG = "#0e1117"
_CHART_PLOT_BG  = "#131722"
_CHART_FONT     = "#d1d4dc"
_CHART_GRID     = "rgba(80,85,105,0.4)"
_CHART_AXIS     = "#4a4f6a"


def _render_chart(
    ticker: str,
    snap: IndicatorSnapshot,
    score: ScoreResult,
) -> None:
    df: Optional[pd.DataFrame] = st.session_state.get(_KEY_INTRADAY_DF)
    if df is None or df.empty:
        st.info("No chart data available yet.")
        return

    df = df.tail(200).copy()  # last ~3.3 hours of 1-min bars
    df.index = pd.to_datetime(df.index)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
        subplot_titles=[f"{ticker} — 1-min Candles", "Volume"],
    )

    # ── Candlesticks ──────────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"], high=df["High"],
            low=df["Low"],   close=df["Close"],
            name="Price",
            increasing_line_color="#26a69a",
            increasing_fillcolor="#26a69a",
            decreasing_line_color="#ef5350",
            decreasing_fillcolor="#ef5350",
            whiskerwidth=0.3,
        ),
        row=1, col=1,
    )

    # ── VWAP ─────────────────────────────────────────────────────────────
    if snap.vwap:
        vwap_series = _recompute_vwap_series(df)
        if vwap_series is not None:
            fig.add_trace(
                go.Scatter(
                    x=df.index, y=vwap_series,
                    name="VWAP",
                    line=dict(color="#ff9800", width=2, dash="dot"),
                    hovertemplate="VWAP: %{y:.2f}<extra></extra>",
                ),
                row=1, col=1,
            )

    # ── SMAs ─────────────────────────────────────────────────────────────
    sma_params = [
        ("SMA20",  20, "#42a5f5", "solid"),
        ("SMA50",  50, "#ce93d8", "solid"),
        ("SMA200", 200, "#ffcc80", "dash"),
    ]
    for name, period, colour, dash in sma_params:
        if len(df) >= period:
            sma = df["Close"].rolling(period).mean()
            fig.add_trace(
                go.Scatter(
                    x=df.index, y=sma,
                    name=name,
                    line=dict(color=colour, width=1.5, dash=dash),
                    hovertemplate=f"{name}: %{{y:.2f}}<extra></extra>",
                ),
                row=1, col=1,
            )

    # ── Support / Resistance ──────────────────────────────────────────────
    for lvl in snap.sr_levels:
        sr_colour = "#26a69a" if lvl.label == "support" else "#ef5350"
        sr_dash   = "dot" if lvl.strength == 1 else "dash"
        label_txt = f"{'S' if lvl.label == 'support' else 'R'} {lvl.price:.2f}"
        fig.add_hline(
            y=lvl.price,
            line=dict(color=sr_colour, width=1, dash=sr_dash),
            annotation_text=label_txt,
            annotation_position="right",
            annotation_font_color=sr_colour,
            annotation_font_size=10,
            annotation_bgcolor="rgba(13,17,23,0.75)",
            row=1, col=1,
        )

    # ── Guidance overlay lines (stop / target1 / target2) ────────────────
    guidance: Optional[GuidanceResult] = st.session_state.get(_KEY_GUIDANCE)
    if guidance and guidance.direction != "none":
        _guide_lines = [
            (guidance.stop_loss,  "#ff1744", f"Stop  {guidance.stop_loss:.2f}" if guidance.stop_loss else None,  "left"),
            (guidance.target1,    "#00e676", f"T1  {guidance.target1:.2f}"      if guidance.target1   else None,  "left"),
            (guidance.target2,    "#69f0ae", f"T2  {guidance.target2:.2f}"      if guidance.target2   else None,  "left"),
        ]
        for price, colour, ann_text, ann_pos in _guide_lines:
            if price:
                fig.add_hline(
                    y=price,
                    line=dict(color=colour, width=1.5, dash="solid"),
                    annotation_text=ann_text,
                    annotation_position=ann_pos,
                    annotation_font_color=colour,
                    annotation_font_size=10,
                    annotation_bgcolor="rgba(13,17,23,0.75)",
                    row=1, col=1,
                )

    # ── Signal markers ───────────────────────────────────────────────────
    if score.dominant_side == "long" and score.long_score >= 60:
        last_price = float(df["Close"].iloc[-1])
        fig.add_trace(
            go.Scatter(
                x=[df.index[-1]], y=[last_price * 0.997],
                mode="markers+text",
                marker=dict(symbol="triangle-up", size=16, color="#00e676",
                            line=dict(color="#ffffff", width=1)),
                text=["LONG"],
                textfont=dict(color="#00e676", size=11),
                textposition="bottom center",
                name="Long Signal",
            ),
            row=1, col=1,
        )
    elif score.dominant_side == "short" and score.short_score >= 60:
        last_price = float(df["Close"].iloc[-1])
        fig.add_trace(
            go.Scatter(
                x=[df.index[-1]], y=[last_price * 1.003],
                mode="markers+text",
                marker=dict(symbol="triangle-down", size=16, color="#ff1744",
                            line=dict(color="#ffffff", width=1)),
                text=["SHORT"],
                textfont=dict(color="#ff1744", size=11),
                textposition="top center",
                name="Short Signal",
            ),
            row=1, col=1,
        )

    # ── Volume bars ───────────────────────────────────────────────────────
    vol_colours = [
        "#26a69a" if c >= o else "#ef5350"
        for c, o in zip(df["Close"], df["Open"])
    ]
    fig.add_trace(
        go.Bar(
            x=df.index, y=df["Volume"],
            name="Volume",
            marker_color=vol_colours,
            showlegend=False,
            opacity=0.75,
            hovertemplate="Vol: %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1,
    )

    # ── Layout ────────────────────────────────────────────────────────────
    fig.update_layout(
        height=620,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.05,
            xanchor="right", x=1,
            bgcolor="rgba(13,17,23,0.8)",
            bordercolor=_CHART_AXIS,
            borderwidth=1,
            font=dict(color=_CHART_FONT, size=11),
        ),
        margin=dict(l=10, r=100, t=55, b=10),
        paper_bgcolor=_CHART_PAPER_BG,
        plot_bgcolor=_CHART_PLOT_BG,
        font=dict(color=_CHART_FONT, size=11),
        hoverlabel=dict(
            bgcolor="rgba(13,17,23,0.9)",
            font_color=_CHART_FONT,
            bordercolor=_CHART_AXIS,
        ),
    )
    # x-axes
    fig.update_xaxes(
        showgrid=True, gridwidth=1, gridcolor=_CHART_GRID,
        linecolor=_CHART_AXIS, tickcolor=_CHART_AXIS,
        tickfont=dict(color=_CHART_FONT, size=10),
        showticklabels=True,
        rangeslider_visible=False,
    )
    # y-axes
    fig.update_yaxes(
        showgrid=True, gridwidth=1, gridcolor=_CHART_GRID,
        linecolor=_CHART_AXIS, tickcolor=_CHART_AXIS,
        tickfont=dict(color=_CHART_FONT, size=10),
    )
    # Volume y-axis: compact unit format (K / M)
    fig.update_yaxes(tickformat=".2s", row=2, col=1)
    # Subplot title colours (layout.annotations)
    for ann in fig.layout.annotations:
        ann.font.color = _CHART_FONT
        ann.font.size  = 12

    st.plotly_chart(fig, use_container_width=True)


def _recompute_vwap_series(df: pd.DataFrame):
    """Recompute VWAP series for chart overlay."""
    try:
        import numpy as np
        last_date = df.index[-1].date()
        today = df[df.index.date == last_date].copy()
        if today.empty:
            return None
        tp = (today["High"] + today["Low"] + today["Close"]) / 3.0
        cum_vol = today["Volume"].cumsum().replace(0, float("nan"))
        vwap = (tp * today["Volume"]).cumsum() / cum_vol
        # Reindex to full df
        return vwap.reindex(df.index)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Guidance panel
# ---------------------------------------------------------------------------

def _render_guidance_panel(
    guidance: Optional[GuidanceResult],
    score: ScoreResult,
    snap: IndicatorSnapshot,
) -> None:
    st.subheader("📐 Trade Guidance")

    if guidance is None or guidance.direction == "none":
        st.info("⚪  No actionable setup — conditions are mixed. Wait for cleaner confluence.")
        return

    col_g1, col_g2, col_g3, col_g4, col_g5 = st.columns(5)
    col_g1.metric("Direction", guidance.direction.upper(),
                  delta=score.confidence_label)
    col_g2.metric("Entry ~", f"${guidance.suggested_entry:.2f}" if guidance.suggested_entry else "—")
    col_g3.metric("Stop",    f"${guidance.stop_loss:.2f}"       if guidance.stop_loss       else "—")
    col_g4.metric("Target 1",f"${guidance.target1:.2f}"         if guidance.target1         else "—")
    col_g5.metric("Target 2",f"${guidance.target2:.2f}"         if guidance.target2         else "—")

    if guidance.reward_risk:
        rr_colour = "normal" if guidance.reward_risk >= 1.5 else "inverse"
        st.metric("Est. Reward/Risk (to T1)", f"{guidance.reward_risk:.2f}x")

    if guidance.entry_basis:
        st.caption(f"Entry basis: {guidance.entry_basis}")
    if guidance.stop_basis:
        st.caption(f"Stop basis: {guidance.stop_basis}")

    if guidance.exit_warning:
        st.warning(f"⚠️  Exit warning: {guidance.exit_warning_reason}")

    st.markdown("&nbsp;")
    for line in guidance.summary_lines:
        st.markdown(line)


# ---------------------------------------------------------------------------
# Indicator metrics row
# ---------------------------------------------------------------------------

def _render_indicators_row(snap: IndicatorSnapshot, quote: Optional[QuoteData]) -> None:
    st.subheader("📊 Indicators")

    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)

    def _fmt(v, decimals=2):
        return f"{v:.{decimals}f}" if v is not None else "—"

    c1.metric("RSI(14)",  _fmt(snap.rsi14, 1))
    c2.metric("MACD",     _fmt(snap.macd_line, 4))
    c3.metric("MACD Sig", _fmt(snap.macd_signal, 4))
    c4.metric("ATR(14)",  _fmt(snap.atr14))
    c5.metric("VWAP",     _fmt(snap.vwap))
    c6.metric("Rel Vol",  _fmt(snap.rel_volume, 1) + "×" if snap.rel_volume else "—")
    c7.metric("Spread %", f"{snap.spread_pct:.3f}%" if snap.spread_pct else "—")
    imb = snap.bid_ask_imbalance
    c8.metric("Bid/Ask imb", f"{imb*100:.0f}% bid" if imb else "—")

    # Second row
    d1, d2, d3, d4, d5, d6 = st.columns(6)
    d1.metric("SMA20",  _fmt(snap.sma20))
    d2.metric("SMA50",  _fmt(snap.sma50))
    d3.metric("SMA200", _fmt(snap.sma200))
    d4.metric("Breakout", "✅ Yes" if snap.breakout else "No")
    d5.metric("Breakdown","✅ Yes" if snap.breakdown else "No")

    sr_near = None
    if snap.nearest_resistance and snap.nearest_support:
        sr_near = (f"S:{snap.nearest_support:.2f}  "
                   f"R:{snap.nearest_resistance:.2f}")
    elif snap.nearest_resistance:
        sr_near = f"R:{snap.nearest_resistance:.2f}"
    elif snap.nearest_support:
        sr_near = f"S:{snap.nearest_support:.2f}"
    d6.metric("Nearest S/R", sr_near or "—")


# ---------------------------------------------------------------------------
# Alerts feed
# ---------------------------------------------------------------------------

def _render_alerts_feed() -> None:
    st.subheader("🔔 Alerts")
    alerts: List[Alert] = st.session_state.get(_KEY_ALERTS, [])
    if not alerts:
        st.caption("No alerts yet this session.")
        return

    for alert in reversed(alerts[-15:]):   # newest first, max 15
        with st.container():
            st.markdown(alert.formatted)


# ---------------------------------------------------------------------------
# Paper-trade log + backtest
# ---------------------------------------------------------------------------

def _render_paper_trade_section(
    pt_logger: PaperTradeLogger,
    ticker: str,
    ibkr,
    provider: IBKRMarketDataProvider,
) -> None:
    tab_log, tab_backtest = st.tabs(["📋 Paper-Trade Log", "🧪 Backtest Simulation"])

    with tab_log:
        st.subheader("Paper-Trade Event Log")
        st.caption(f"Log file: `{pt_logger.filepath}`")
        rows = pt_logger.read_log()
        if rows:
            df_log = pd.DataFrame(rows)
            # Filter to current ticker
            if ticker and "ticker" in df_log.columns:
                df_log = df_log[df_log["ticker"] == ticker]
            st.dataframe(df_log, use_container_width=True, height=300)
        else:
            st.info("No paper-trade events logged yet.")

    with tab_backtest:
        st.subheader("🧪 Backtest Simulation")
        st.caption(
            "Replay historical 1-min bars through the scoring engine to validate "
            "the signal logic.  Results are educational only."
        )

        bt_col1, bt_col2, bt_col3 = st.columns(3)
        bt_ticker = bt_col1.text_input("Ticker", value=ticker, key="_igt_bt_ticker")
        bt_date   = bt_col2.date_input(
            "Date", value=date.today() - timedelta(days=1), key="_igt_bt_date"
        )
        bt_min_score = bt_col3.slider(
            "Min signal score", min_value=40, max_value=90, value=60, key="_igt_bt_min"
        )

        if st.button("▶️ Run Backtest", key="_igt_run_bt"):
            with st.spinner(f"Running backtest for {bt_ticker} on {bt_date} …"):
                try:
                    bt = IntradayBacktester(
                        provider=provider,
                        min_signal_score=float(bt_min_score),
                        log_to_csv=True,
                    )
                    result = bt.run(
                        symbol=bt_ticker.upper(),
                        target_date=bt_date,
                    )
                    st.success("Backtest complete!")
                    st.text(result.summary())

                    if result.trades:
                        trades_df = pd.DataFrame(
                            [
                                {
                                    "timestamp": t.timestamp,
                                    "direction": t.direction,
                                    "entry": t.entry,
                                    "stop": t.stop,
                                    "target1": t.target1,
                                    "target2": t.target2,
                                    "exit": t.exit_price,
                                    "exit_reason": t.exit_reason,
                                    "pnl": round(t.pnl or 0, 4),
                                    "confidence": t.confidence_label,
                                }
                                for t in result.trades
                            ]
                        )
                        st.dataframe(trades_df, use_container_width=True)
                except Exception as exc:
                    st.error(f"Backtest error: {exc}")
                    logger.error("Backtest failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Demo / offline mode
# ---------------------------------------------------------------------------

def _render_demo_mode(ticker: str) -> None:
    """Show a placeholder UI when IBKR is not connected."""
    st.markdown("---")
    st.markdown("### Demo / Offline Preview")
    st.info(
        "When IBKR is connected, this panel shows:\n"
        "- Live bid/ask, last price, sizes\n"
        "- 1-min OHLCV candlestick chart with VWAP, SMA20/50/200, S/R lines\n"
        "- Long / Short probability scores (0–100)\n"
        "- Confidence label: Strong Long → Strong Short\n"
        "- Entry, stop, target 1 & 2, reward/risk\n"
        "- RSI, MACD, ATR, relative volume, spread, bid/ask imbalance\n"
        "- Real-time alert feed\n"
        "- Paper-trade log + backtesting tab"
    )

    # Synthetic demo chart
    import numpy as np
    np.random.seed(42)
    n = 80
    prices = 200 + np.cumsum(np.random.randn(n) * 0.3)
    idx = pd.date_range(end=pd.Timestamp.now(), periods=n, freq="1min")
    opens  = prices + np.random.randn(n) * 0.15
    closes = prices + np.random.randn(n) * 0.15
    highs  = np.maximum(opens, closes) + abs(np.random.randn(n) * 0.1)
    lows   = np.minimum(opens, closes) - abs(np.random.randn(n) * 0.1)
    vols   = np.random.randint(5000, 50000, n)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.04,
        subplot_titles=[f"{ticker} — Demo (IBKR not connected)", "Volume"],
    )
    vol_colours = [
        "#26a69a" if c >= o else "#ef5350"
        for c, o in zip(closes, opens)
    ]
    fig.add_trace(go.Candlestick(
        x=idx, open=opens, high=highs, low=lows, close=closes,
        name=f"{ticker} (demo)",
        increasing_line_color="#26a69a", increasing_fillcolor="#26a69a",
        decreasing_line_color="#ef5350", decreasing_fillcolor="#ef5350",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=idx, y=pd.Series(closes).rolling(20).mean(),
        name="SMA20 (demo)",
        line=dict(color="#42a5f5", width=1.5),
        hovertemplate="SMA20: %{y:.2f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=idx, y=vols, name="Volume",
        marker_color=vol_colours, showlegend=False, opacity=0.75,
    ), row=2, col=1)
    fig.update_layout(
        height=460,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.05,
            xanchor="right", x=1,
            bgcolor="rgba(13,17,23,0.8)",
            font=dict(color=_CHART_FONT, size=11),
        ),
        margin=dict(l=10, r=30, t=55, b=10),
        paper_bgcolor=_CHART_PAPER_BG,
        plot_bgcolor=_CHART_PLOT_BG,
        font=dict(color=_CHART_FONT, size=11),
    )
    fig.update_xaxes(
        showgrid=True, gridcolor=_CHART_GRID,
        linecolor=_CHART_AXIS, tickcolor=_CHART_AXIS,
        tickfont=dict(color=_CHART_FONT, size=10),
        rangeslider_visible=False,
    )
    fig.update_yaxes(
        showgrid=True, gridcolor=_CHART_GRID,
        linecolor=_CHART_AXIS, tickcolor=_CHART_AXIS,
        tickfont=dict(color=_CHART_FONT, size=10),
    )
    fig.update_yaxes(tickformat=".2s", row=2, col=1)
    for ann in fig.layout.annotations:
        ann.font.color = _CHART_FONT
        ann.font.size  = 12
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _init_session_state() -> None:
    """Initialise all session-state keys with safe defaults."""
    if _KEY_TICKER not in st.session_state:
        st.session_state[_KEY_TICKER] = ""
    if _KEY_ALERT_ENG not in st.session_state:
        st.session_state[_KEY_ALERT_ENG] = AlertEngine()
    if _KEY_PT_LOGGER not in st.session_state:
        st.session_state[_KEY_PT_LOGGER] = PaperTradeLogger()
    if _KEY_ALERTS not in st.session_state:
        st.session_state[_KEY_ALERTS] = []
    if _KEY_REFRESH_TS not in st.session_state:
        st.session_state[_KEY_REFRESH_TS] = 0
