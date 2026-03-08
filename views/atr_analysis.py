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

    # ── ATR Probability Model (computed once, used in chart + section) ─────────
    prob_model = _compute_atr_probability_model(
        current_price, current_atr, trend_bias, pcr_signal, pcr_data
    )

    # ── Volatility compression ────────────────────────────────────────────────
    vol_compression = _detect_vol_compression(df, atr_col)

    # ── Target hit probabilities ──────────────────────────────────────────────
    rr_ratio = target_dist / stop_dist  # always 2.0 with current 1:2 setup
    long_prob,  _ = _probability_of_hit(abs(long_target  - current_price) / current_atr)
    short_prob, _ = _probability_of_hit(abs(short_target - current_price) / current_atr)

    # ── Trade quality scores ──────────────────────────────────────────────────
    long_score  = _compute_trade_score("long",  trend_bias, rr_ratio, long_prob,  pcr_signal)
    short_score = _compute_trade_score("short", trend_bias, rr_ratio, short_prob, pcr_signal)

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

    # ATR probability bands overlaid on candlestick
    fig = _add_atr_bands_to_chart(fig, prob_model)

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

    # ── Quant Probability Model ───────────────────────────────────────────────
    _render_quant_model(
        prob_model, current_price, current_atr,
        vol_compression,
        long_target, short_target, long_prob, short_prob,
        long_score, short_score,
    )

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


# ─────────────────────────────────────────────────────────────────────────────
# ATR Probability Model
# ─────────────────────────────────────────────────────────────────────────────

_PROB_BANDS = [
    (0.5, 38),
    (1.0, 68),
    (1.5, 86),
    (2.0, 95),
]


def _compute_atr_probability_model(
    current_price: float,
    current_atr: float,
    trend_bias: str,
    pcr_signal: str,
    pcr_data: dict,
) -> dict:
    """Compute ATR-based probability ranges and key levels."""
    bands = [
        {
            "multiplier":  mult,
            "probability": prob,
            "high": round(current_price + current_atr * mult, 2),
            "low":  round(current_price - current_atr * mult, 2),
        }
        for mult, prob in _PROB_BANDS
    ]

    # Bias from price trend
    if trend_bias == "bearish":
        bias = "Lean Short"
    elif trend_bias == "bullish":
        bias = "Lean Long"
    else:
        bias = "Neutral"

    # Options sentiment — use PCR volume range 0.8–1.2 as neutral
    pcr_vol = pcr_data.get("pcr_volume")
    if pcr_vol is not None and 0.8 <= pcr_vol <= 1.2:
        options_sentiment = "Neutral"
    elif pcr_signal == "bullish":
        options_sentiment = "Bullish — Lean Long"
    elif pcr_signal == "bearish":
        options_sentiment = "Bearish — Lean Short"
    else:
        options_sentiment = "Neutral"

    return {
        "atr_probability_ranges": bands,
        "most_probable_range":    bands[1],                                      # 1.0 ATR = 68%
        "expected_high":   round(current_price + current_atr * 1.0, 2),
        "expected_low":    round(current_price - current_atr * 1.0, 2),
        "extended_high":   round(current_price + current_atr * 1.5, 2),
        "extended_low":    round(current_price - current_atr * 1.5, 2),
        "extreme_high":    round(current_price + current_atr * 2.0, 2),
        "extreme_low":     round(current_price - current_atr * 2.0, 2),
        "vol_breakout_up": round(current_price + current_atr * 3.0, 2),
        "vol_breakout_dn": round(current_price - current_atr * 3.0, 2),
        "bias":             bias,
        "options_sentiment": options_sentiment,
    }


def _render_atr_probability_model(model: dict, current_price: float, current_atr: float):
    """Render the ATR Probability Model section."""
    st.markdown("### 🎲 ATR Probability Model")
    st.caption(
        "Probability bands derived from ATR-based volatility cone.  "
        "1 ATR ≈ 68% of expected daily price movement (1σ equivalent).  "
        "3× ATR marks volatility breakout / momentum continuation levels."
    )

    # ── Probability band cards ────────────────────────────────────────────────
    band_meta = [
        ("0.5 ATR",  "Tight (38%)",         "#2c2c4a"),
        ("1.0 ATR",  "Most Probable (68%)", "#1a3a5c"),
        ("1.5 ATR",  "Extended (86%)",      "#1a4a2a"),
        ("2.0 ATR",  "Extreme (95%)",       "#4a1a1a"),
    ]
    cols = st.columns(4)
    for col, (label, subtitle, bg), band in zip(cols, band_meta, model["atr_probability_ranges"]):
        rng_w = band["high"] - band["low"]
        col.markdown(
            f'<div style="background:{bg};border-radius:12px;padding:16px 12px;'
            f'text-align:center;font-family:monospace;border:1px solid #ffffff18">'
            f'<div style="color:#00d4ff;font-size:0.75rem;font-weight:bold;letter-spacing:1px;'
            f'text-transform:uppercase;margin-bottom:4px">{label}</div>'
            f'<div style="color:#aaa;font-size:0.65rem;margin-bottom:10px">{subtitle}</div>'
            f'<div style="color:#44ff88;font-size:1.15rem;font-weight:bold">'
            f'&#9650; ${band["high"]:.2f}</div>'
            f'<div style="color:#888;font-size:0.7rem;margin:4px 0">'
            f'Range: ${rng_w:.2f}</div>'
            f'<div style="color:#ff6644;font-size:1.15rem;font-weight:bold">'
            f'&#9660; ${band["low"]:.2f}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Most probable next-day range callout ──────────────────────────────────
    mp = model["most_probable_range"]
    rng_w = mp["high"] - mp["low"]
    st.info(
        f"**Most Probable Next-Day Range (68%):** "
        f"${mp['low']:.2f} — ${mp['high']:.2f}  |  "
        f"Range width: ${rng_w:.2f}  |  "
        f"Centre: ${current_price:.2f}"
    )

    st.markdown("#### 📍 Key Levels")
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Expected High",   f"${model['expected_high']:.2f}",
              f"+${current_atr:.2f}", delta_color="normal")
    k2.metric("Expected Low",    f"${model['expected_low']:.2f}",
              f"-${current_atr:.2f}", delta_color="inverse")
    k3.metric("Extreme High",    f"${model['extreme_high']:.2f}",
              f"+${current_atr*2:.2f}", delta_color="normal")
    k4.metric("Extreme Low",     f"${model['extreme_low']:.2f}",
              f"-${current_atr*2:.2f}", delta_color="inverse")
    k5.metric("Vol Breakout ↑",  f"${model['vol_breakout_up']:.2f}",
              f"+${current_atr*3:.2f}", delta_color="normal")
    k6.metric("Vol Breakout ↓",  f"${model['vol_breakout_dn']:.2f}",
              f"-${current_atr*3:.2f}", delta_color="inverse")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Bias + Options Sentiment ──────────────────────────────────────────────
    b1, b2 = st.columns(2)
    bias = model["bias"]
    bias_color = "#28a745" if "Long" in bias else ("#dc3545" if "Short" in bias else "#6c757d")
    b1.markdown(
        f'<div style="background:{bias_color}22;border:1px solid {bias_color};'
        f'border-radius:8px;padding:14px">'
        f'<div style="color:#aaa;font-size:0.65rem;text-transform:uppercase;'
        f'letter-spacing:1px;margin-bottom:4px">Price Trend Bias</div>'
        f'<div style="color:{bias_color};font-size:1.3rem;font-weight:bold">{bias}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    sent = model["options_sentiment"]
    sent_color = ("#28a745" if "Long" in sent
                  else "#dc3545" if "Short" in sent
                  else "#6c757d")
    b2.markdown(
        f'<div style="background:{sent_color}22;border:1px solid {sent_color};'
        f'border-radius:8px;padding:14px">'
        f'<div style="color:#aaa;font-size:0.65rem;text-transform:uppercase;'
        f'letter-spacing:1px;margin-bottom:4px">Options Sentiment (PCR)</div>'
        f'<div style="color:{sent_color};font-size:1.3rem;font-weight:bold">{sent}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Volatility Compression Detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_vol_compression(df: pd.DataFrame, atr_col: str) -> dict:
    """Detect ATR compression: 3+ consecutive sessions of falling ATR."""
    atrs = df[atr_col].dropna().values
    if len(atrs) < 3:
        return {"compressed": False, "sessions": 0, "flag": "", "pct_change": 0.0}

    consecutive = 0
    for i in range(len(atrs) - 1, 0, -1):
        if atrs[i] < atrs[i - 1]:
            consecutive += 1
        else:
            break

    ref_idx    = max(0, len(atrs) - 1 - consecutive)
    ref_atr    = float(atrs[ref_idx])
    curr_atr   = float(atrs[-1])
    pct_change = round((curr_atr - ref_atr) / ref_atr * 100, 1) if ref_atr else 0.0
    compressed = consecutive >= 3

    return {
        "compressed": compressed,
        "sessions":   consecutive,
        "flag":       "⚡ Breakout Likely" if compressed else "",
        "pct_change": pct_change,
        "ref_atr":    round(ref_atr, 2),
        "curr_atr":   round(curr_atr, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Probability of Price Target Hit
# ─────────────────────────────────────────────────────────────────────────────

def _probability_of_hit(distance_atr: float) -> tuple:
    """
    ATR-normalised distance → approximate probability of target being hit.
    Returns (probability_float, label_str).
    """
    if distance_atr <= 0.5:
        return 60.0, "~60%"
    elif distance_atr <= 1.0:
        return 35.0, "~35%"
    elif distance_atr <= 1.5:
        return 14.0, "~14%"
    elif distance_atr <= 2.0:
        return 5.0,  "~5%"
    else:
        return 2.0,  "<2%"


# ─────────────────────────────────────────────────────────────────────────────
# Trade Quality Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _compute_trade_score(
    direction:   str,    # "long" or "short"
    trend_bias:  str,    # bullish / bearish / neutral
    rr_ratio:    float,  # reward-to-risk ratio
    prob_of_hit: float,  # probability % (0-100)
    pcr_signal:  str,    # bullish / bearish / neutral
) -> dict:
    """Score a trade 0–100 across 4 weighted components."""
    trend_aligned = (direction == "long"  and trend_bias == "bullish") or \
                    (direction == "short" and trend_bias == "bearish")
    trend_opposed = (direction == "long"  and trend_bias == "bearish") or \
                    (direction == "short" and trend_bias == "bullish")
    trend_pts   = 40 if trend_aligned else (10 if trend_opposed else 25)
    trend_label = "✅ Aligned" if trend_aligned else ("❌ Opposed" if trend_opposed else "➖ Neutral")

    rr_pts   = 30 if rr_ratio >= 2.0 else (20 if rr_ratio >= 1.5 else 10)
    rr_label = f"{rr_ratio:.1f}:1"

    prob_pts   = 20 if prob_of_hit >= 40 else (10 if prob_of_hit >= 20 else 5)
    prob_label = f"{prob_of_hit:.0f}%"

    pcr_aligned = (direction == "long"  and pcr_signal == "bullish") or \
                  (direction == "short" and pcr_signal == "bearish")
    pcr_opposed = (direction == "long"  and pcr_signal == "bearish") or \
                  (direction == "short" and pcr_signal == "bullish")
    pcr_pts   = 10 if pcr_aligned else (0 if pcr_opposed else 5)
    pcr_label = "✅ Aligned" if pcr_aligned else ("❌ Opposed" if pcr_opposed else "➖ Neutral")

    total = trend_pts + rr_pts + prob_pts + pcr_pts

    if total >= 80:
        label, badge_color = "High Conviction Trade", "#00c851"
    elif total >= 60:
        label, badge_color = "Good Setup",            "#ffbb33"
    elif total >= 40:
        label, badge_color = "Moderate Setup",        "#ff8800"
    else:
        label, badge_color = "Low Probability",       "#ff4444"

    return {
        "score":       total,
        "label":       label,
        "badge_color": badge_color,
        "components": {
            "trend": {"pts": trend_pts, "max": 40, "detail": trend_label},
            "rr":    {"pts": rr_pts,    "max": 30, "detail": rr_label},
            "prob":  {"pts": prob_pts,  "max": 20, "detail": prob_label},
            "pcr":   {"pts": pcr_pts,   "max": 10, "detail": pcr_label},
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Quant Probability Model — master render
# ─────────────────────────────────────────────────────────────────────────────

def _render_quant_model(
    prob_model:      dict,
    current_price:   float,
    current_atr:     float,
    vol_compression: dict,
    long_target:     float,
    short_target:    float,
    long_prob:       float,
    short_prob:      float,
    long_score:      dict,
    short_score:     dict,
):
    """Render the full Quantitative Trading Model section."""

    st.markdown("## 🧮 Quantitative Trading Model")
    st.caption(
        "Probabilistic price range model using ATR-based volatility distribution.  "
        "All levels update dynamically with each analysis run."
    )

    # ── SECTION 1: Quant Probability Model (ATR bands) ────────────────────────
    st.markdown("### 📊 Quant Probability Model")
    st.caption("Probability of price staying within each ATR band by end of session.")

    band_meta = [
        ("0.5 ATR",  "Tight — 38%",         "#1e1e3a", "#00d4ff"),
        ("1.0 ATR",  "Most Probable — 68%", "#0d2137", "#44cc88"),
        ("1.5 ATR",  "Extended — 86%",      "#0d2b10", "#88dd44"),
        ("2.0 ATR",  "Extreme — 95%",       "#2b0d0d", "#ff8844"),
    ]
    cols = st.columns(4)
    for col, (label, subtitle, bg, accent), band in zip(
        cols, band_meta, prob_model["atr_probability_ranges"]
    ):
        rng_w = band["high"] - band["low"]
        col.markdown(
            f'<div style="background:{bg};border-radius:12px;padding:18px 14px;'
            f'text-align:center;font-family:monospace;border:1.5px solid {accent}44;'
            f'box-shadow:0 2px 12px rgba(0,0,0,0.4)">'
            f'<div style="color:{accent};font-size:0.78rem;font-weight:bold;'
            f'letter-spacing:1px;text-transform:uppercase;margin-bottom:2px">{label}</div>'
            f'<div style="color:#888;font-size:0.65rem;margin-bottom:12px">{subtitle}</div>'
            f'<div style="color:#44ff88;font-size:1.15rem;font-weight:bold">'
            f'&#9650; ${band["high"]:.2f}</div>'
            f'<div style="color:#555;font-size:0.68rem;margin:4px 0">'
            f'width: ${rng_w:.2f}</div>'
            f'<div style="color:#ff6644;font-size:1.15rem;font-weight:bold">'
            f'&#9660; ${band["low"]:.2f}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)
    mp   = prob_model["most_probable_range"]
    rng_w = mp["high"] - mp["low"]
    st.info(
        f"**Most Probable Range (68%):** &nbsp; "
        f"**Low** ${mp['low']:.2f} — **High** ${mp['high']:.2f} &nbsp;|&nbsp; "
        f"Width: ${rng_w:.2f} &nbsp;|&nbsp; Centre: ${current_price:.2f}"
    )

    st.markdown("---")

    # ── SECTION 2: Expected Next Day Levels ───────────────────────────────────
    st.markdown("### 📍 Expected Next Day Levels")
    l1, l2, l3, l4, l5, l6 = st.columns(6)
    l1.metric("Expected High",    f"${prob_model['expected_high']:.2f}",
              f"+${current_atr:.2f}", delta_color="normal")
    l2.metric("Expected Low",     f"${prob_model['expected_low']:.2f}",
              f"-${current_atr:.2f}", delta_color="inverse")
    l3.metric("Max Intraday High", f"${prob_model['extended_high']:.2f}",
              f"+${current_atr*1.5:.2f}", delta_color="normal")
    l4.metric("Max Intraday Low",  f"${prob_model['extended_low']:.2f}",
              f"-${current_atr*1.5:.2f}", delta_color="inverse")
    l5.metric("Extreme High",     f"${prob_model['extreme_high']:.2f}",
              f"+${current_atr*2:.2f}", delta_color="normal")
    l6.metric("Extreme Low",      f"${prob_model['extreme_low']:.2f}",
              f"-${current_atr*2:.2f}", delta_color="inverse")

    st.markdown("---")

    # ── SECTION 3: Volatility Breakout Detection ──────────────────────────────
    st.markdown("### ⚡ Volatility Breakout Detection")
    vc = vol_compression
    if vc["compressed"]:
        st.warning(
            f"**ATR Compression Detected** — ATR has fallen for "
            f"**{vc['sessions']} consecutive sessions** "
            f"(from ${vc['ref_atr']:.2f} → ${vc['curr_atr']:.2f}, "
            f"{vc['pct_change']:.1f}%)  |  {vc['flag']}"
        )
        vc1, vc2, vc3 = st.columns(3)
        vc1.metric("Compression Sessions", str(vc["sessions"]))
        vc2.metric("ATR Change",           f"{vc['pct_change']:.1f}%", delta_color="inverse")
        vc3.metric("Breakout Signal",      "YES ⚡", delta_color="normal")
    else:
        sessions = vc["sessions"]
        msg = (
            f"No compression detected (ATR falling for {sessions} session(s) — need 3+)."
            if sessions > 0 else
            "No ATR compression. Volatility is expanding or stable."
        )
        st.success(f"✅ Volatility normal — {msg}")
        if sessions > 0:
            vc1, vc2 = st.columns(2)
            vc1.metric("Declining Sessions", str(sessions), f"need 3 to trigger")
            vc2.metric("ATR Change",         f"{vc['pct_change']:.1f}%", delta_color="inverse")

    # Vol breakout levels
    st.caption(
        f"Volatility Breakout Levels:  "
        f"↑ ${prob_model['vol_breakout_up']:.2f} (+3× ATR)  |  "
        f"↓ ${prob_model['vol_breakout_dn']:.2f} (−3× ATR)"
    )

    st.markdown("---")

    # ── SECTION 4: Target Probabilities ──────────────────────────────────────
    st.markdown("### 🎯 Target Probabilities")
    st.caption(
        "Approximate probability of price reaching each trade target, "
        "based on ATR-distance from current price."
    )
    tp1, tp2 = st.columns(2)
    with tp1:
        long_dist_atr = abs(long_target - current_price) / current_atr
        color_l = "#44ff88" if long_prob >= 35 else ("#ffaa00" if long_prob >= 14 else "#ff6644")
        st.markdown(
            f'<div style="background:#0a2010;border:1.5px solid {color_l};'
            f'border-radius:10px;padding:18px">'
            f'<div style="color:#aaa;font-size:0.7rem;text-transform:uppercase;'
            f'letter-spacing:1px;margin-bottom:6px">&#128994; Long Target</div>'
            f'<div style="color:white;font-size:1.1rem;font-weight:bold;margin-bottom:4px">'
            f'${long_target:.2f} &nbsp;<span style="color:#888;font-size:0.8rem">'
            f'({long_dist_atr:.2f}× ATR away)</span></div>'
            f'<div style="color:{color_l};font-size:2rem;font-weight:bold">{long_prob:.0f}%</div>'
            f'<div style="color:#888;font-size:0.7rem">probability of target hit</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with tp2:
        short_dist_atr = abs(short_target - current_price) / current_atr
        color_s = "#44ff88" if short_prob >= 35 else ("#ffaa00" if short_prob >= 14 else "#ff6644")
        st.markdown(
            f'<div style="background:#200a0a;border:1.5px solid {color_s};'
            f'border-radius:10px;padding:18px">'
            f'<div style="color:#aaa;font-size:0.7rem;text-transform:uppercase;'
            f'letter-spacing:1px;margin-bottom:6px">&#128308; Short Target</div>'
            f'<div style="color:white;font-size:1.1rem;font-weight:bold;margin-bottom:4px">'
            f'${short_target:.2f} &nbsp;<span style="color:#888;font-size:0.8rem">'
            f'({short_dist_atr:.2f}× ATR away)</span></div>'
            f'<div style="color:{color_s};font-size:2rem;font-weight:bold">{short_prob:.0f}%</div>'
            f'<div style="color:#888;font-size:0.7rem">probability of target hit</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── SECTION 5: Trade Quality Score ───────────────────────────────────────
    st.markdown("### 🏆 Trade Quality Score")
    st.caption(
        "Composite score 0–100 across: Trend alignment (40 pts), "
        "Risk/Reward (30 pts), Target probability (20 pts), Options sentiment (10 pts)."
    )

    _render_score_card("🟢 Long Setup",  long_score)
    _render_score_card("🔴 Short Setup", short_score)

    # Bias + Sentiment
    st.markdown("<br>", unsafe_allow_html=True)
    b1, b2 = st.columns(2)
    bias = prob_model["bias"]
    bias_color = "#28a745" if "Long" in bias else ("#dc3545" if "Short" in bias else "#6c757d")
    b1.markdown(
        f'<div style="background:{bias_color}22;border:1px solid {bias_color};'
        f'border-radius:8px;padding:14px">'
        f'<div style="color:#888;font-size:0.65rem;text-transform:uppercase;'
        f'letter-spacing:1px;margin-bottom:4px">Price Trend Bias</div>'
        f'<div style="color:{bias_color};font-size:1.3rem;font-weight:bold">{bias}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    sent = prob_model["options_sentiment"]
    sent_color = "#28a745" if "Long" in sent else ("#dc3545" if "Short" in sent else "#6c757d")
    b2.markdown(
        f'<div style="background:{sent_color}22;border:1px solid {sent_color};'
        f'border-radius:8px;padding:14px">'
        f'<div style="color:#888;font-size:0.65rem;text-transform:uppercase;'
        f'letter-spacing:1px;margin-bottom:4px">Options Sentiment (PCR)</div>'
        f'<div style="color:{sent_color};font-size:1.3rem;font-weight:bold">{sent}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_score_card(title: str, score_data: dict):
    """Render a compact trade quality score card with breakdown bar."""
    score  = score_data["score"]
    label  = score_data["label"]
    color  = score_data["badge_color"]
    comps  = score_data["components"]

    # Score gauge using a simple Plotly indicator
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        title={"text": f"<b>{title}</b>", "font": {"size": 13}},
        gauge={
            "axis": {"range": [0, 100], "tickvals": [40, 60, 80, 100]},
            "bar":  {"color": color},
            "steps": [
                {"range": [0,  40], "color": "#3a1010"},
                {"range": [40, 60], "color": "#3a2a10"},
                {"range": [60, 80], "color": "#1a3a1a"},
                {"range": [80,100], "color": "#0a2a0a"},
            ],
            "threshold": {
                "line": {"color": "white", "width": 3},
                "thickness": 0.85,
                "value": score,
            },
        },
        number={"suffix": "/100", "valueformat": ".0f", "font": {"color": color, "size": 28}},
    ))
    fig.update_layout(height=220, margin=dict(t=30, b=0, l=20, r=20),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")

    g_col, b_col = st.columns([1, 1])
    with g_col:
        st.plotly_chart(fig, use_container_width=True)
    with b_col:
        st.markdown(
            f'<div style="padding:10px 0;margin-top:20px">'
            f'<div style="background:{color};color:#000;border-radius:6px;'
            f'padding:6px 12px;font-weight:bold;font-size:0.9rem;'
            f'display:inline-block;margin-bottom:12px">{label}</div>',
            unsafe_allow_html=True,
        )
        comp_labels = {
            "trend": "Trend Alignment",
            "rr":    "Risk / Reward",
            "prob":  "Target Probability",
            "pcr":   "Options Sentiment",
        }
        for key, meta in comps.items():
            pct = int(meta["pts"] / meta["max"] * 100)
            bar_color = "#44ff88" if pct >= 75 else ("#ffaa00" if pct >= 50 else "#ff6644")
            st.markdown(
                f'<div style="margin-bottom:6px">'
                f'<div style="font-size:0.65rem;color:#aaa;margin-bottom:2px">'
                f'{comp_labels[key]} — {meta["detail"]} ({meta["pts"]}/{meta["max"]} pts)</div>'
                f'<div style="background:#222;border-radius:4px;height:8px">'
                f'<div style="background:{bar_color};width:{pct}%;height:8px;border-radius:4px"></div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)


def _add_atr_bands_to_chart(fig, model: dict):
    """Overlay ATR probability band lines on the candlestick chart (row 1)."""
    lines = [
        (model["expected_high"],   "Exp High 68%",   "rgba(68,255,136,0.85)",  "dash",    1.5),
        (model["expected_low"],    "Exp Low 68%",    "rgba(255,100,68,0.85)",   "dash",    1.5),
        (model["extended_high"],   "Ext High 86%",   "rgba(68,255,136,0.55)",   "dot",     1.2),
        (model["extended_low"],    "Ext Low 86%",    "rgba(255,100,68,0.55)",   "dot",     1.2),
        (model["extreme_high"],    "Extreme High 95%","rgba(255,255,68,0.6)",   "dashdot", 1.2),
        (model["extreme_low"],     "Extreme Low 95%", "rgba(255,165,0,0.6)",    "dashdot", 1.2),
        (model["vol_breakout_up"], "Vol Break ↑ 3×", "rgba(200,100,255,0.7)",  "dot",     1.0),
        (model["vol_breakout_dn"], "Vol Break ↓ 3×", "rgba(200,100,255,0.7)",  "dot",     1.0),
    ]
    for price, name, color, dash, width in lines:
        fig.add_hline(
            y=price, line_dash=dash, line_color=color, line_width=width,
            annotation_text=name, annotation_font_size=9,
            annotation_font_color=color,
            row=1, col=1,
        )
    return fig


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
