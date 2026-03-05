"""
ATR Analysis Page - Average True Range analysis for long/short entry levels.

Shows the last N days of OHLCV + ATR data and computes optimal
entry, stop-loss, and take-profit levels for both long and short setups.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta

from connectors.yahoo_finance import YahooFinanceConnector
from analysis.indicators import calculate_atr, calculate_sma
from core.logging_manager import get_logger

logger = get_logger("dashboard.atr_analysis")


def render():
    st.title("📐 ATR Analysis — Long / Short Levels")
    st.write("Average True Range analysis to find optimal entry, stop-loss, and target levels.")

    # ── Controls ──────────────────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        symbol = st.text_input("Symbol", value="NVDA").upper().strip()
    with col2:
        lookback_days = st.selectbox(
            "Lookback Period",
            [10, 14, 21, 30],
            index=1,
            format_func=lambda x: f"{x} trading days (~{x * 7 // 5} weeks)"
        )
    with col3:
        atr_period = st.selectbox("ATR Period", [7, 10, 14, 20], index=2)

    atr_multiplier = st.slider(
        "ATR Multiplier for stops / targets",
        min_value=0.5, max_value=4.0, value=1.5, step=0.25,
        help="Stop-loss = ATR × multiplier away from entry. Target = 2× that distance."
    )

    if st.button("Run ATR Analysis", type="primary"):
        run_atr_analysis(symbol, lookback_days, atr_period, atr_multiplier)


def run_atr_analysis(symbol: str, lookback_days: int, atr_period: int, atr_multiplier: float):
    with st.spinner(f"Fetching price data and options chain for {symbol}…"):
        yf_conn = YahooFinanceConnector()
        # Fetch extra days so ATR has enough history to warm up
        fetch_days = lookback_days + atr_period + 10
        raw = yf_conn.get_historical_data(symbol, days=fetch_days)
        pcr_data = yf_conn.get_put_call_ratio(symbol, max_expiries=4)

    if raw is None or raw.empty:
        st.error(f"No data returned for {symbol}. Check the symbol and try again.")
        return
    # ── Calculate ATR ─────────────────────────────────────────────────────────
    df = calculate_atr(raw, period=atr_period)
    atr_col = f"ATR_{atr_period}"

    # Keep only the last `lookback_days` rows after ATR warm-up
    df = df.dropna(subset=[atr_col]).tail(lookback_days).reset_index(drop=True)

    if df.empty:
        st.error("Not enough data to compute ATR. Try a longer lookback or smaller ATR period.")
        return

    # ── Normalise date column ─────────────────────────────────────────────────
    date_col = pd.to_datetime(df["date"])
    if hasattr(date_col.dtype, "tz") and date_col.dtype.tz is not None:
        date_col = date_col.dt.tz_convert(None)
    df["date"] = date_col.dt.date  # date only – no time

    # ── Pre-market close = prior day close (shifted) ──────────────────────────
    df["pre_market_close"] = df["close"].shift(1)

    # ── Build display table ───────────────────────────────────────────────────
    display_df = pd.DataFrame({
        "Date":            df["date"],
        "Opening":         df["open"].round(2),
        "Closing":         df["close"].round(2),
        "Pre-Market Close": df["pre_market_close"].round(2),
        "ATR":             df[atr_col].round(2),
        "Min (Low)":       df["low"].round(2),
        "Max (High)":      df["high"].round(2),
    })

    st.markdown("### 📊 Market Data & ATR Analysis")

    # Style: blue header row to match the screenshot
    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Date":             st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
            "Opening":          st.column_config.NumberColumn("Opening",  format="%.2f"),
            "Closing":          st.column_config.NumberColumn("Closing",  format="%.2f"),
            "Pre-Market Close": st.column_config.NumberColumn("Pre-Market Close", format="%.2f"),
            "ATR":              st.column_config.NumberColumn("ATR",      format="%.2f"),
            "Min (Low)":        st.column_config.NumberColumn("Min (Low)", format="%.2f"),
            "Max (High)":       st.column_config.NumberColumn("Max (High)",format="%.2f"),
        }
    )

    st.markdown("---")

    # ── Current-day signals ───────────────────────────────────────────────────
    latest      = df.iloc[-1]
    current_price = float(latest["close"])
    current_atr   = float(latest[atr_col])
    avg_atr       = float(df[atr_col].mean())

    stop_dist   = current_atr * atr_multiplier
    target_dist = stop_dist * 2.0  # 2:1 risk-reward

    long_entry  = current_price
    long_stop   = round(long_entry - stop_dist, 2)
    long_target = round(long_entry + target_dist, 2)

    short_entry  = current_price
    short_stop   = round(short_entry + stop_dist, 2)
    short_target = round(short_entry - target_dist, 2)

    atr_pct = (current_atr / current_price) * 100
    trend_bias = _trend_bias(df)
    pcr_signal = pcr_data.get("signal", "neutral") if "error" not in pcr_data else "neutral"

    # ── Combined recommendation ───────────────────────────────────────────────
    _render_combined_recommendation(trend_bias, pcr_signal, pcr_data)

    st.markdown("---")
    st.markdown(f"### 🎯 Entry Levels for **{symbol}** — based on last {len(df)}-day ATR")

    m1, m2, m3 = st.columns(3)
    m1.metric("Current Price",  f"${current_price:.2f}")
    m2.metric(f"ATR ({atr_period}d)",  f"${current_atr:.2f}",  f"{atr_pct:.1f}% of price")
    m3.metric("Avg ATR (period)", f"${avg_atr:.2f}",
              "Rising ↑" if current_atr > avg_atr else "Falling ↓",
              delta_color="off")

    st.markdown("---")

    long_col, short_col = st.columns(2)

    with long_col:
        st.markdown("#### 🟢 Long Setup")
        bias_flag = "✅ Trend agrees" if trend_bias == "bullish" else ("⚠️ Counter-trend" if trend_bias == "bearish" else "")
        pcr_flag  = "✅ PCR agrees"   if pcr_signal  == "bullish" else ("⚠️ PCR disagrees" if pcr_signal == "bearish" else "")
        st.markdown(f"**Price Bias:** {trend_bias.capitalize()}  {bias_flag}")
        st.markdown(f"**PCR Signal:** {pcr_signal.capitalize()}  {pcr_flag}")
        _level_card(
            entry=long_entry, stop=long_stop, target=long_target,
            stop_dist=stop_dist, target_dist=target_dist,
            direction="long"
        )

    with short_col:
        st.markdown("#### 🔴 Short Setup")
        bias_flag = "✅ Trend agrees" if trend_bias == "bearish" else ("⚠️ Counter-trend" if trend_bias == "bullish" else "")
        pcr_flag  = "✅ PCR agrees"   if pcr_signal  == "bearish" else ("⚠️ PCR disagrees" if pcr_signal == "bullish" else "")
        st.markdown(f"**Price Bias:** {trend_bias.capitalize()}  {bias_flag}")
        st.markdown(f"**PCR Signal:** {pcr_signal.capitalize()}  {pcr_flag}")
        _level_card(
            entry=short_entry, stop=short_stop, target=short_target,
            stop_dist=stop_dist, target_dist=target_dist,
            direction="short"
        )

    st.markdown("---")

    # ── ATR trend chart ───────────────────────────────────────────────────────
    st.markdown("### 📈 Price & ATR Chart")

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.68, 0.32],
        subplot_titles=[f"{symbol} Price (Candlestick)", f"ATR ({atr_period}-period)"],
        vertical_spacing=0.08
    )

    dts = df["date"].astype(str)

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=dts, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="Price",
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350"
    ), row=1, col=1)

    # Long entry zone
    fig.add_hline(y=long_target, line_dash="dot", line_color="green",
                  annotation_text="Long Target", row=1, col=1)
    fig.add_hline(y=current_price, line_dash="solid", line_color="royalblue",
                  annotation_text="Price", row=1, col=1)
    fig.add_hline(y=long_stop, line_dash="dot", line_color="red",
                  annotation_text="Long Stop", row=1, col=1)

    # ATR line
    fig.add_trace(go.Scatter(
        x=dts, y=df[atr_col], name=f"ATR {atr_period}",
        line=dict(color="orange", width=2), fill="tozeroy",
        fillcolor="rgba(255,165,0,0.15)"
    ), row=2, col=1)

    # Avg ATR reference
    fig.add_hline(y=avg_atr, line_dash="dash", line_color="gray",
                  annotation_text="Avg ATR", row=2, col=1)

    fig.update_layout(
        height=580,
        showlegend=False,
        xaxis_rangeslider_visible=False,
        margin=dict(t=40, b=20)
    )

    st.plotly_chart(fig, width="stretch")

    # ── Put / Call Ratio section ──────────────────────────────────────────────
    _render_pcr_section(pcr_data)

    st.markdown("---")

    # ── ATR summary table ─────────────────────────────────────────────────────
    st.markdown("### 📋 ATR-Based Level Summary")

    summary_df = pd.DataFrame([
        {
            "Setup":       "🟢 Long",
            "Entry":       f"${long_entry:.2f}",
            "Stop-Loss":   f"${long_stop:.2f}",
            "Target":      f"${long_target:.2f}",
            "Risk $":      f"${stop_dist:.2f}",
            "Reward $":    f"${target_dist:.2f}",
            "R:R":         "1:2",
            "ATR × mult":  f"{atr_multiplier}×",
        },
        {
            "Setup":       "🔴 Short",
            "Entry":       f"${short_entry:.2f}",
            "Stop-Loss":   f"${short_stop:.2f}",
            "Target":      f"${short_target:.2f}",
            "Risk $":      f"${stop_dist:.2f}",
            "Reward $":    f"${target_dist:.2f}",
            "R:R":         "1:2",
            "ATR × mult":  f"{atr_multiplier}×",
        },
    ])
    st.dataframe(summary_df, width="stretch", hide_index=True)

    st.caption(
        f"Levels calculated as: Entry ± ATR({atr_period}) × {atr_multiplier} for stop, "
        f"± ATR × {atr_multiplier * 2:.1f} for target. Risk-reward fixed at 1:2."
    )


def _level_card(entry: float, stop: float, target: float,
                stop_dist: float, target_dist: float, direction: str):
    """Render a metric card for a single setup."""
    c1, c2, c3 = st.columns(3)
    c1.metric("Entry",  f"${entry:.2f}")
    if direction == "long":
        c2.metric("Stop-Loss",  f"${stop:.2f}",  f"-${stop_dist:.2f}",   delta_color="inverse")
        c3.metric("Target",     f"${target:.2f}", f"+${target_dist:.2f}", delta_color="normal")
    else:
        c2.metric("Stop-Loss",  f"${stop:.2f}",  f"+${stop_dist:.2f}",   delta_color="inverse")
        c3.metric("Target",     f"${target:.2f}", f"-${target_dist:.2f}", delta_color="normal")


def _trend_bias(df: pd.DataFrame) -> str:
    """Simple bias: compare last close to 10-day SMA."""
    try:
        sma_df = calculate_sma(df, period=min(10, len(df)))
        sma_col = [c for c in sma_df.columns if c.startswith("SMA_")]
        if not sma_col:
            return "neutral"
        last_close = float(sma_df["close"].iloc[-1])
        last_sma   = float(sma_df[sma_col[0]].iloc[-1])
        if last_close > last_sma * 1.005:
            return "bullish"
        elif last_close < last_sma * 0.995:
            return "bearish"
        return "neutral"
    except Exception:
        return "neutral"


def _render_combined_recommendation(trend_bias: str, pcr_signal: str, pcr_data: dict):
    """Show a single high-level strategy recommendation combining price trend + PCR."""
    st.markdown("### 🧭 Strategy Recommendation")

    signals = [trend_bias, pcr_signal]
    bull_votes = signals.count("bullish")
    bear_votes = signals.count("bearish")

    if bull_votes == 2:
        rec, color, icon = "LONG", "green", "🟢"
        desc = "Both price trend and options sentiment are bullish — strong long setup."
    elif bear_votes == 2:
        rec, color, icon = "SHORT", "red", "🔴"
        desc = "Both price trend and options sentiment are bearish — strong short setup."
    elif bull_votes > bear_votes:
        rec, color, icon = "LEAN LONG", "green", "🟡"
        desc = "Mixed signals but bullish edge — favour long with tighter stops."
    elif bear_votes > bull_votes:
        rec, color, icon = "LEAN SHORT", "red", "🟡"
        desc = "Mixed signals but bearish edge — favour short with tighter stops."
    else:
        rec, color, icon = "NEUTRAL / WAIT", "gray", "⚪"
        desc = "No clear directional edge. Wait for confirmation before entering."

    pcr_vol = pcr_data.get("pcr_volume")
    pcr_detail = pcr_data.get("signal_detail", "")

    st.markdown(
        f"""
        <div style="border:2px solid {color}; border-radius:8px; padding:16px; margin-bottom:12px;">
            <h3 style="color:{color}; margin:0">{icon} {rec}</h3>
            <p style="margin:6px 0 0 0">{desc}</p>
            {"<p style='margin:4px 0 0 0; font-size:0.9em; color:#666'><b>PCR detail:</b> " + pcr_detail + "</p>" if pcr_detail else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )

    sc1, sc2 = st.columns(2)
    sc1.metric("Price Trend", trend_bias.capitalize())
    sc2.metric("PCR Sentiment", pcr_signal.capitalize(),
               f"{pcr_vol:.2f}" if pcr_vol else "N/A",
               delta_color="off")


def _render_pcr_section(pcr_data: dict):
    """Render the full Put/Call Ratio analysis section."""
    st.markdown("### 📊 Put / Call Ratio (Options Sentiment)")

    if "error" in pcr_data:
        st.warning(f"Could not fetch options data: {pcr_data['error']}")
        return

    pcr_vol = pcr_data.get("pcr_volume")
    pcr_oi  = pcr_data.get("pcr_oi")
    signal  = pcr_data.get("signal", "neutral")
    detail  = pcr_data.get("signal_detail", "")

    # Top metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("PCR (Volume)",       f"{pcr_vol:.3f}" if pcr_vol else "N/A",
              help="Put volume / Call volume across near-term expiries")
    m2.metric("PCR (Open Interest)", f"{pcr_oi:.3f}"  if pcr_oi  else "N/A",
              help="Put OI / Call OI across near-term expiries")
    m3.metric("Total Put Vol",  f"{pcr_data.get('total_put_vol', 0):,}")
    m4.metric("Total Call Vol", f"{pcr_data.get('total_call_vol', 0):,}")

    # Signal badge
    color_map = {"bullish": "#28a745", "bearish": "#dc3545", "neutral": "#6c757d"}
    badge_color = color_map.get(signal, "#6c757d")
    st.markdown(
        f'<div style="background:{badge_color};color:white;padding:10px 16px;'
        f'border-radius:6px;margin:8px 0"><b>Sentiment: {signal.upper()}</b>'
        f" — {detail}</div>",
        unsafe_allow_html=True,
    )

    # PCR gauge chart
    if pcr_vol is not None:
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=pcr_vol,
            title={"text": "PCR (Volume)"},
            gauge={
                "axis": {"range": [0, 2.0], "tickvals": [0.6, 0.8, 1.0, 1.2, 1.5, 2.0]},
                "bar":  {"color": badge_color},
                "steps": [
                    {"range": [0,   0.6],  "color": "#d4edda"},   # bullish zone
                    {"range": [0.6, 0.8],  "color": "#c8e6c9"},
                    {"range": [0.8, 1.0],  "color": "#fff3cd"},   # neutral zone
                    {"range": [1.0, 1.2],  "color": "#ffd0b5"},
                    {"range": [1.2, 2.0],  "color": "#f8d7da"},   # bearish zone
                ],
                "threshold": {
                    "line": {"color": "black", "width": 3},
                    "thickness": 0.8,
                    "value": pcr_vol,
                },
            },
            number={"suffix": "", "valueformat": ".3f"},
        ))
        fig.update_layout(height=260, margin=dict(t=30, b=10, l=20, r=20))
        st.plotly_chart(fig, width="stretch")

    # Per-expiry breakdown
    rows = pcr_data.get("expiry_breakdown", [])
    if rows:
        st.markdown("**By Expiry Date**")
        exp_df = pd.DataFrame(rows)
        st.dataframe(
            exp_df,
            width="stretch",
            hide_index=True,
            column_config={
                "Put Vol":   st.column_config.NumberColumn("Put Vol",  format="%d"),
                "Call Vol":  st.column_config.NumberColumn("Call Vol", format="%d"),
                "PCR (Vol)": st.column_config.NumberColumn("PCR (Vol)", format="%.3f"),
                "Put OI":    st.column_config.NumberColumn("Put OI",   format="%d"),
                "Call OI":   st.column_config.NumberColumn("Call OI",  format="%d"),
                "PCR (OI)":  st.column_config.NumberColumn("PCR (OI)", format="%.3f"),
            },
        )

    st.caption(
        "PCR < 0.8 → Bullish (more calls than puts)  |  "
        "PCR 0.8–1.0 → Neutral  |  "
        "PCR > 1.0 → Bearish (more puts than calls)  |  "
        "Extreme fear: PCR > 1.5 (potential contrarian long)"
    )
