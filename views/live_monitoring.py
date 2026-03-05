"""
Live Monitoring Page – real-time watchlist, positions, P&L and signal alerts.

Data sources:
- IBKR connector (positions, account summary) when connected
- Yahoo Finance (live quotes, enriched prices) always available
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

from connectors.yahoo_finance import YahooFinanceConnector
from analysis.indicators import calculate_rsi, calculate_atr

# ─────────────────────────────────────────────────────────────────────────────
# Default watchlist (mirrors universe.yaml default + swing candidates)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "AMZN", "GOOGL"
]

_UNIVERSE_GROUPS = {
    "Tech / Mega-cap": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"],
    "Index ETFs": ["SPY", "QQQ", "IWM", "DIA"],
    "Swing Candidates": ["AAPL", "MSFT", "AMD", "NVDA", "TSLA", "BA", "F", "GE"],
    "Financials / Energy": ["JPM", "V", "XLF", "XLE"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Session state helpers
# ─────────────────────────────────────────────────────────────────────────────

def _init_state():
    if "mon_watchlist" not in st.session_state:
        st.session_state.mon_watchlist = list(_DEFAULT_WATCHLIST)
    if "mon_alerts" not in st.session_state:
        st.session_state.mon_alerts: List[Dict] = []   # {symbol, condition, price, note}
    if "mon_last_refresh" not in st.session_state:
        st.session_state.mon_last_refresh = None
    if "mon_quote_cache" not in st.session_state:
        st.session_state.mon_quote_cache: Dict[str, Dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_quotes(symbols: List[str]) -> Dict[str, Dict]:
    """Batch-fetch quotes via yfinance."""
    if not symbols:
        return {}
    quotes: Dict[str, Dict] = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                t = tickers.tickers[sym]
                info = t.info
                hist = t.history(period="5d", auto_adjust=True)

                price = (
                    info.get("currentPrice")
                    or info.get("regularMarketPrice")
                    or info.get("previousClose")
                )
                prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")

                if price is None and not hist.empty:
                    price = float(hist["Close"].iloc[-1])
                if prev_close is None and len(hist) >= 2:
                    prev_close = float(hist["Close"].iloc[-2])

                change = (price - prev_close) if price and prev_close else None
                pct    = (change / prev_close * 100) if change is not None and prev_close else None

                quotes[sym] = {
                    "price":       round(float(price), 2) if price else None,
                    "prev_close":  round(float(prev_close), 2) if prev_close else None,
                    "change":      round(change, 2) if change is not None else None,
                    "pct_change":  round(pct, 2) if pct is not None else None,
                    "volume":      info.get("volume") or (int(hist["Volume"].iloc[-1]) if not hist.empty else None),
                    "avg_volume":  info.get("averageVolume"),
                    "day_high":    info.get("dayHigh"),
                    "day_low":     info.get("dayLow"),
                }
            except Exception:
                quotes[sym] = {"price": None}
    except Exception as e:
        st.warning(f"Quote fetch error: {e}")
    return quotes


def _fetch_signal(symbol: str) -> Dict:
    """Return RSI + trend signal for a symbol using last 30 days of data."""
    try:
        df = yf.download(symbol, period="30d", auto_adjust=True, progress=False)
        if df.empty or len(df) < 15:
            return {"rsi": None, "trend": "—", "signal": "—"}

        df = df.reset_index()
        df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]

        # RSI
        rsi_df = calculate_rsi(df.copy(), period=14)
        rsi_col = [c for c in rsi_df.columns if "rsi" in c.lower()]
        rsi_val = float(rsi_df[rsi_col[0]].dropna().iloc[-1]) if rsi_col else None

        # Trend: close vs 10-SMA
        close = df["close"].dropna()
        sma10 = close.rolling(10).mean().iloc[-1]
        last  = close.iloc[-1]
        trend = "Bullish" if last > sma10 else ("Bearish" if last < sma10 else "Neutral")

        # Signal composite
        if rsi_val is not None:
            if rsi_val < 35 and trend == "Bullish":
                sig = "BUY"
            elif rsi_val > 65 and trend == "Bearish":
                sig = "SELL"
            elif rsi_val < 45 and trend == "Bullish":
                sig = "WATCH LONG"
            elif rsi_val > 55 and trend == "Bearish":
                sig = "WATCH SHORT"
            else:
                sig = "NEUTRAL"
        else:
            sig = "—"

        return {"rsi": round(rsi_val, 1) if rsi_val else None, "trend": trend, "signal": sig}
    except Exception:
        return {"rsi": None, "trend": "—", "signal": "—"}


def _signal_color(sig: str) -> str:
    m = {"BUY": "🟢", "WATCH LONG": "🟡", "NEUTRAL": "⚪", "WATCH SHORT": "🟠", "SELL": "🔴"}
    return m.get(sig, "⚪")


def _pct_arrow(pct: Optional[float]) -> str:
    if pct is None:
        return "—"
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "■")
    color = "green" if pct > 0 else ("red" if pct < 0 else "grey")
    return f":{color}[{arrow} {abs(pct):.2f}%]"


def _vol_ratio(vol, avg_vol) -> str:
    if not vol or not avg_vol:
        return "—"
    r = vol / avg_vol
    label = f"{r:.1f}x"
    if r >= 2:
        return f"🔥 {label}"
    if r >= 1.5:
        return f"⚡ {label}"
    return label


# ─────────────────────────────────────────────────────────────────────────────
# Section renderers
# ─────────────────────────────────────────────────────────────────────────────

def _render_header():
    col_title, col_refresh = st.columns([3, 1])
    with col_title:
        st.title("📈 Live Monitoring")
    with col_refresh:
        last = st.session_state.mon_last_refresh
        if last:
            st.caption(f"Last refreshed: {last.strftime('%H:%M:%S')}")
        interval = st.selectbox(
            "Auto-refresh",
            ["Off", "30 s", "60 s", "120 s"],
            key="mon_refresh_interval",
            label_visibility="collapsed",
        )
    return interval


def _render_connection_bar(ibkr):
    connected = ibkr is not None and ibkr.is_connected()
    if connected:
        accounts = ibkr.ib.managedAccounts() if ibkr.ib else []
        acc_str   = ", ".join(accounts) if accounts else "unknown"
        mode_str  = "PAPER" if ibkr.port in (7497, 4002) else "LIVE"
        st.success(f"✅ IBKR {mode_str} connected · Account: **{acc_str}**")
    else:
        st.warning("⚠️ IBKR not connected — showing Yahoo Finance data only.  Go to **Configuration** to connect.")
    return connected


def _render_account_summary(ibkr, connected: bool):
    st.subheader("Account Summary")
    if not connected:
        st.info("Connect to IBKR to see live account data.")
        return

    with st.spinner("Loading account data…"):
        summary = ibkr.get_account_summary()

    if not summary:
        st.info("No account data available.")
        return

    key_map = {
        "netliquidation":          ("Net Liquidation", "$"),
        "totalcashvalue":          ("Cash",            "$"),
        "buyingpower":             ("Buying Power",    "$"),
        "unrealizedpnl":           ("Unrealized P&L",  "$"),
        "realizedpnl":             ("Realized P&L",    "$"),
        "grosspositionvalue":      ("Position Value",  "$"),
        "availablefunds":          ("Available Funds", "$"),
        "equitywithloancalue":     ("Equity",          "$"),
    }

    display = {}
    for raw_key, (label, prefix) in key_map.items():
        for k, v in summary.items():
            if raw_key in k.lower():
                display[label] = v
                break

    if not display:
        st.json(summary)
        return

    cols = st.columns(min(len(display), 4))
    for i, (label, val) in enumerate(display.items()):
        with cols[i % 4]:
            color = "normal"
            if "P&L" in label:
                color = "normal" if val >= 0 else "inverse"
            delta = None
            if "P&L" in label:
                delta = f"${val:,.2f}"
                val_display = ""
            else:
                val_display = f"${val:,.2f}"
            st.metric(label, val_display if val_display else "—", delta=delta if delta else None)


def _render_positions(ibkr, connected: bool, yf_conn: YahooFinanceConnector):
    st.subheader("Current Positions")

    if not connected:
        st.info("Connect to IBKR to see live positions.")
        return

    with st.spinner("Loading positions…"):
        positions = ibkr.get_positions()

    if not positions:
        st.info("No open positions.")
        return

    # Enrich with live prices from yfinance where IBKR doesn't supply them
    symbols = [p["symbol"] for p in positions]
    live_quotes = _fetch_quotes(symbols)

    rows = []
    for pos in positions:
        sym    = pos["symbol"]
        qty    = pos["position"]
        avg    = pos["avg_cost"]
        quote  = live_quotes.get(sym, {})
        mp     = pos.get("market_price") or quote.get("price")
        mv     = pos.get("market_value") or (mp * qty if mp else None)
        upnl   = pos.get("unrealized_pnl")
        if upnl is None and mp and avg:
            upnl = (mp - avg) * qty
        pct_chg = quote.get("pct_change")

        rows.append({
            "Symbol":        sym,
            "Shares":        int(qty),
            "Avg Cost":      f"${avg:.2f}" if avg else "—",
            "Last Price":    f"${mp:.2f}" if mp else "—",
            "Day Change":    _pct_arrow(pct_chg),
            "Market Value":  f"${mv:,.2f}" if mv else "—",
            "Unrealized P&L": f"${upnl:,.2f}" if upnl is not None else "—",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Total unrealized P&L
    total_upnl = sum(
        p.get("unrealized_pnl") or 0
        for p in positions
        if p.get("unrealized_pnl") is not None
    )
    color = "green" if total_upnl >= 0 else "red"
    st.markdown(f"**Total Unrealized P&L:** :{color}[${total_upnl:,.2f}]")


def _render_watchlist(yf_conn: YahooFinanceConnector):
    st.subheader("Watchlist")

    # ── Watchlist editor ─────────────────────────────────────────────────────
    with st.expander("Manage Watchlist", expanded=False):
        add_col, remove_col, preset_col = st.columns(3)

        with add_col:
            new_sym = st.text_input("Add symbol", key="mon_add_sym", placeholder="e.g. GOOG")
            if st.button("➕ Add", key="mon_add_btn"):
                sym_clean = new_sym.strip().upper()
                if sym_clean and sym_clean not in st.session_state.mon_watchlist:
                    st.session_state.mon_watchlist.append(sym_clean)
                    st.rerun()

        with remove_col:
            if st.session_state.mon_watchlist:
                to_remove = st.selectbox(
                    "Remove symbol", ["—"] + st.session_state.mon_watchlist, key="mon_remove_sym"
                )
                if st.button("➖ Remove", key="mon_remove_btn") and to_remove != "—":
                    st.session_state.mon_watchlist.remove(to_remove)
                    st.rerun()

        with preset_col:
            preset = st.selectbox("Load preset", ["—"] + list(_UNIVERSE_GROUPS.keys()), key="mon_preset")
            if st.button("Load", key="mon_load_preset") and preset != "—":
                st.session_state.mon_watchlist = list(_UNIVERSE_GROUPS[preset])
                st.rerun()

    watchlist = st.session_state.mon_watchlist
    if not watchlist:
        st.info("Watchlist is empty. Add symbols above.")
        return

    with st.spinner(f"Fetching quotes for {len(watchlist)} symbols…"):
        quotes = _fetch_quotes(watchlist)
        st.session_state.mon_quote_cache = quotes

    # Signal computation — run in sequence (rate-limited)
    signals_col = st.empty()
    with signals_col:
        with st.spinner("Calculating signals…"):
            signals = {sym: _fetch_signal(sym) for sym in watchlist}

    # Check alerts against live prices
    triggered = _check_alerts(quotes)

    # ── Table ─────────────────────────────────────────────────────────────────
    rows = []
    for sym in watchlist:
        q   = quotes.get(sym, {})
        sig = signals.get(sym, {})
        price     = q.get("price")
        pct_chg   = q.get("pct_change")
        alert_hit = sym in triggered

        rows.append({
            "🔔":           "🔔" if alert_hit else "",
            "Symbol":       sym,
            "Last":         f"${price:.2f}" if price else "—",
            "Change":       f"{'+' if (q.get('change') or 0) >= 0 else ''}{q.get('change'):.2f}" if q.get("change") is not None else "—",
            "% Chg":        f"{'+' if (pct_chg or 0) >= 0 else ''}{pct_chg:.2f}%" if pct_chg is not None else "—",
            "Volume":       _vol_ratio(q.get("volume"), q.get("avg_volume")),
            "Day Hi":       f"${q['day_high']:.2f}" if q.get("day_high") else "—",
            "Day Lo":       f"${q['day_low']:.2f}" if q.get("day_low") else "—",
            "RSI(14)":      f"{sig['rsi']:.1f}" if sig.get("rsi") else "—",
            "Trend":        sig.get("trend", "—"),
            "Signal":       f"{_signal_color(sig.get('signal','—'))} {sig.get('signal','—')}",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Mini sparklines ───────────────────────────────────────────────────────
    with st.expander("Sparklines (5-day)", expanded=False):
        spark_cols = st.columns(min(len(watchlist), 5))
        for i, sym in enumerate(watchlist[:10]):  # cap at 10
            with spark_cols[i % 5]:
                try:
                    hist = yf.download(sym, period="5d", auto_adjust=True, progress=False)
                    if not hist.empty:
                        fig = go.Figure(
                            go.Scatter(
                                x=hist.index,
                                y=hist["Close"].squeeze(),
                                mode="lines",
                                line=dict(
                                    width=2,
                                    color="green" if hist["Close"].iloc[-1] >= hist["Close"].iloc[0] else "red",
                                ),
                            )
                        )
                        fig.update_layout(
                            height=80,
                            margin=dict(l=0, r=0, t=0, b=0),
                            xaxis=dict(visible=False),
                            yaxis=dict(visible=False),
                            showlegend=False,
                            paper_bgcolor="rgba(0,0,0,0)",
                            plot_bgcolor="rgba(0,0,0,0)",
                        )
                        st.markdown(f"**{sym}**")
                        st.plotly_chart(fig, use_container_width=True, key=f"spark_{sym}")
                except Exception:
                    pass


def _render_alerts():
    st.subheader("Price Alerts")

    with st.expander("Add Alert", expanded=False):
        a_col1, a_col2, a_col3, a_col4 = st.columns(4)
        with a_col1:
            a_sym = st.text_input("Symbol", key="alert_sym", placeholder="SPY")
        with a_col2:
            a_cond = st.selectbox("Condition", ["≥ (above)", "≤ (below)"], key="alert_cond")
        with a_col3:
            a_price = st.number_input("Price ($)", key="alert_price", min_value=0.0, step=0.5)
        with a_col4:
            a_note = st.text_input("Note (optional)", key="alert_note")

        if st.button("➕ Add Alert", key="add_alert_btn"):
            sym_clean = a_sym.strip().upper()
            if sym_clean and a_price > 0:
                st.session_state.mon_alerts.append({
                    "symbol":    sym_clean,
                    "condition": "above" if "≥" in a_cond else "below",
                    "price":     a_price,
                    "note":      a_note,
                    "triggered": False,
                })
                st.success(f"Alert added: {sym_clean} {a_cond} ${a_price:.2f}")
                st.rerun()

    quotes   = st.session_state.mon_quote_cache
    alerts   = st.session_state.mon_alerts
    triggered = _check_alerts(quotes)

    if not alerts:
        st.info("No alerts set. Use the form above to add price alerts.")
        return

    rows = []
    for idx, a in enumerate(alerts):
        price_now = (quotes.get(a["symbol"]) or {}).get("price")
        hit       = a["symbol"] in triggered
        rows.append({
            "#":         idx,
            "Status":    "🔔 TRIGGERED" if hit else "🕐 Watching",
            "Symbol":    a["symbol"],
            "Condition": f"{'≥' if a['condition']=='above' else '≤'} ${a['price']:.2f}",
            "Last Price": f"${price_now:.2f}" if price_now else "—",
            "Note":      a.get("note", ""),
        })

    df = pd.DataFrame(rows).drop(columns=["#"])
    st.dataframe(df, use_container_width=True, hide_index=True)

    remove_idx = st.number_input("Remove alert # (0-based index)", min_value=0,
                                  max_value=max(len(alerts) - 1, 0), step=1, key="remove_alert_idx")
    if st.button("🗑️ Remove Alert", key="remove_alert_btn") and alerts:
        st.session_state.mon_alerts.pop(int(remove_idx))
        st.rerun()

    # Show banner if anything triggered
    if triggered:
        for sym in triggered:
            a_data = next((a for a in alerts if a["symbol"] == sym), None)
            if a_data:
                st.error(f"🔔 Alert triggered: **{sym}** is {a_data['condition']} ${a_data['price']:.2f}")


def _check_alerts(quotes: Dict[str, Dict]) -> List[str]:
    """Return list of symbols whose alerts have been triggered."""
    triggered = []
    for a in st.session_state.mon_alerts:
        sym   = a["symbol"]
        price = (quotes.get(sym) or {}).get("price")
        if price is None:
            continue
        if a["condition"] == "above" and price >= a["price"]:
            triggered.append(sym)
        elif a["condition"] == "below" and price <= a["price"]:
            triggered.append(sym)
    return triggered


def _render_signal_heatmap(signals: Optional[Dict] = None):
    """Mini heatmap of all watchlist signals."""
    if not signals:
        return

    order = ["BUY", "WATCH LONG", "NEUTRAL", "WATCH SHORT", "SELL", "—"]
    color_map = {
        "BUY": "#00cc44",
        "WATCH LONG": "#88cc00",
        "NEUTRAL": "#888888",
        "WATCH SHORT": "#cc8800",
        "SELL": "#cc2200",
        "—": "#444444",
    }

    rows = sorted(signals.items(), key=lambda x: order.index(x[1].get("signal", "—")))

    cols = st.columns(min(len(rows), 5))
    for i, (sym, sig) in enumerate(rows):
        with cols[i % 5]:
            color = color_map.get(sig.get("signal", "—"), "#444444")
            sig_label = sig.get("signal", "—")
            rsi_str = f"RSI {sig['rsi']:.0f}" if sig.get("rsi") else ""
            st.markdown(
                f"""<div style="
                    background:{color};color:white;border-radius:8px;
                    padding:8px;text-align:center;margin:2px;font-size:0.8rem
                ">
                <b>{sym}</b><br>{sig_label}<br><small>{rsi_str}</small>
                </div>""",
                unsafe_allow_html=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Main render entry point
# ─────────────────────────────────────────────────────────────────────────────

def render():
    _init_state()
    yf_conn = YahooFinanceConnector()

    # ibkr comes from session state (set in streamlit_app.py)
    ibkr = st.session_state.get("ibkr")

    interval = _render_header()
    connected = _render_connection_bar(ibkr)

    st.divider()

    # Tabs
    tab_positions, tab_watchlist, tab_alerts, tab_signals = st.tabs(
        ["📂 Positions & Account", "👁️ Watchlist", "🔔 Alerts", "📊 Signal Board"]
    )

    with tab_positions:
        _render_account_summary(ibkr, connected)
        st.divider()
        _render_positions(ibkr, connected, yf_conn)

    with tab_watchlist:
        _render_watchlist(yf_conn)

    with tab_alerts:
        _render_alerts()

    with tab_signals:
        st.subheader("Signal Board")
        st.caption("Real-time composite signals based on RSI(14) and 10-day SMA trend.")
        watchlist = st.session_state.mon_watchlist
        if watchlist:
            with st.spinner("Computing signals…"):
                signals = {sym: _fetch_signal(sym) for sym in watchlist}
            st.divider()
            _render_signal_heatmap(signals)
            st.divider()
            # Sortable table
            rows = [
                {
                    "Symbol": sym,
                    "RSI": sig.get("rsi"),
                    "Trend": sig.get("trend", "—"),
                    "Signal": sig.get("signal", "—"),
                }
                for sym, sig in signals.items()
            ]
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("Add symbols to the Watchlist tab first.")

    # ── Auto-refresh ─────────────────────────────────────────────────────────
    st.session_state.mon_last_refresh = datetime.now()

    if interval != "Off":
        seconds = int(interval.split()[0])
        st.caption(f"⏱ Auto-refreshing every {seconds} seconds. Last: {datetime.now().strftime('%H:%M:%S')}")
        time.sleep(seconds)
        st.rerun()
