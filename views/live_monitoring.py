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
from connectors.finviz_universe import FinvizEliteUniverse
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
    # ── Legacy key kept for backward compat (ignored now) ─────────────────────
    if "mon_watchlist" not in st.session_state:
        st.session_state.mon_watchlist = []       # computed; do not edit directly
    if "mon_alerts" not in st.session_state:
        st.session_state.mon_alerts: List[Dict] = []
    if "mon_last_refresh" not in st.session_state:
        st.session_state.mon_last_refresh = None
    if "mon_quote_cache" not in st.session_state:
        st.session_state.mon_quote_cache: Dict[str, Dict] = {}
    # ── Finviz-managed tickers ─────────────────────────────────────────────────
    if "mon_finviz_tickers" not in st.session_state:
        st.session_state.mon_finviz_tickers: List[str] = []     # from Finviz scoring
    if "mon_excluded_finviz" not in st.session_state:
        st.session_state.mon_excluded_finviz: set = set()        # user-deselected Finviz picks
    # ── User-managed custom tickers ────────────────────────────────────────────
    if "mon_custom_tickers" not in st.session_state:
        st.session_state.mon_custom_tickers: List[str] = list(_DEFAULT_WATCHLIST)  # start with defaults
    # ── IBKR live quote cache (from get_live_quotes) ───────────────────────────
    if "mon_ibkr_quotes" not in st.session_state:
        st.session_state.mon_ibkr_quotes: Dict[str, Dict] = {}
    # ── Dynamic universe (Finviz Elite) ────────────────────────────────────────
    if "finviz_universe" not in st.session_state:
        st.session_state.finviz_universe = FinvizEliteUniverse()
    if "dyn_universe_enabled" not in st.session_state:
        st.session_state.dyn_universe_enabled = False
    if "dyn_universe_scored_df" not in st.session_state:
        st.session_state.dyn_universe_scored_df = None
    if "dyn_universe_last_loaded" not in st.session_state:
        st.session_state.dyn_universe_last_loaded = None


def _get_active_watchlist() -> List[str]:
    """
    Returns the combined, deduplicated active ticker list used by the
    Watchlist table and Signal Board:
      • Active Finviz picks  (minus any the user has excluded)
      • Custom tickers       (always included, deduplicated)
    """
    excluded = st.session_state.get("mon_excluded_finviz", set())
    finviz   = [t for t in st.session_state.get("mon_finviz_tickers", []) if t not in excluded]
    custom   = st.session_state.get("mon_custom_tickers", [])
    seen: set = set()
    result: List[str] = []
    for t in finviz + custom:
        if t not in seen:
            seen.add(t)
            result.append(t)
    # Keep mon_watchlist in sync for any code that still reads it
    st.session_state.mon_watchlist = result
    return result


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
    m = {"BUY": "🟢", "WATCH LONG": "🟡", "NEUTRAL": "⚪", "WATCH SHORT": "🟠", "SELL": "🔴",
         "STRONG_LONG": "🟢", "LONG": "🟢", "SHORT": "🔴", "STRONG_SHORT": "🔴"}
    return m.get(sig, "⚪")


# ───────────────────────────────────────────────────────────────────────────────
# IBKR live-signal calculator  (ported from dashboard.py ˙ calculate_short_signal)
# ───────────────────────────────────────────────────────────────────────────────

def calculate_ibkr_signal(data: Dict) -> tuple:
    """
    Mean-reversion signal based on IBKR live tick data.
    Detects both dips (LONG) and peaks (SHORT) using intraday range, momentum,
    bid/ask spread, and volume.

    Ported verbatim from dashboard.py ˙ TickerMonitor.calculate_short_signal.

    Returns
    -------
    (signal, strength)
      signal   : 'STRONG_LONG' | 'LONG' | 'NEUTRAL' | 'SHORT' | 'STRONG_SHORT'
      strength : int -100 .. +100  (negative = bullish, positive = bearish)
    """
    last   = data.get("last")  or 0
    high   = data.get("high")  or 0
    low    = data.get("low")   or 0
    bid    = data.get("bid")   or 0
    ask    = data.get("ask")   or 0
    volume = data.get("volume") or 0
    change = data.get("change_pct") or 0

    if last == 0 or high == 0 or low == 0:
        return "NEUTRAL", 0

    price_range = high - low
    if price_range == 0:
        return "NEUTRAL", 0

    price_pos = (last - low) / price_range   # 0 = at low, 1 = at high

    long_score  = 0
    short_score = 0

    # ── Dip detection (LONG) ───────────────────────────────────
    if   price_pos < 0.15:  long_score += 35
    elif price_pos < 0.25:  long_score += 25
    elif price_pos < 0.35:  long_score += 15

    if   change < -2.5:     long_score += 30
    elif change < -1.5:     long_score += 20
    elif change < -0.5:     long_score += 10

    # ── Peak detection (SHORT) ──────────────────────────────────
    if   price_pos > 0.85:  short_score += 35
    elif price_pos > 0.75:  short_score += 25
    elif price_pos > 0.65:  short_score += 15

    if   change > 2.5:      short_score += 30
    elif change > 1.5:      short_score += 20
    elif change > 0.5:      short_score += 10

    # ── Common: spread + volume ───────────────────────────────────
    liq_bonus = 0
    if bid > 0 and ask > 0:
        sp_pct = (ask - bid) / last * 100 if last > 0 else 0
        liq_bonus = 20 if sp_pct < 0.1 else (10 if sp_pct < 0.2 else 0)

    vol_bonus = 0
    if   volume > 1_000_000:  vol_bonus = 15
    elif volume > 500_000:    vol_bonus = 8

    long_score  += liq_bonus + vol_bonus
    short_score += liq_bonus + vol_bonus

    if   long_score > short_score  and long_score  >= 60: return "STRONG_LONG",  -long_score
    elif long_score > short_score  and long_score  >= 40: return "LONG",         -long_score
    elif short_score > long_score  and short_score >= 60: return "STRONG_SHORT",  short_score
    elif short_score > long_score  and short_score >= 40: return "SHORT",         short_score
    else:                                                 return "NEUTRAL", 0


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
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            st.success(f"✅ IBKR {mode_str} connected · Account: **{acc_str}** · Port: {ibkr.port}")
        with col_btn:
            if st.button("Disconnect", key="mon_ibkr_disconnect"):
                ibkr.disconnect()
                if "ibkr_connected" in st.session_state:
                    st.session_state.ibkr_connected = False
                st.rerun()
    else:
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            host = getattr(ibkr, 'host', '127.0.0.1') if ibkr else '127.0.0.1'
            port = getattr(ibkr, 'port', 4002) if ibkr else 4002
            st.warning(f"⚠️ IBKR not connected (target: {host}:{port}) — showing Yahoo Finance data only.")
        with col_btn:
            if st.button("Connect", key="mon_ibkr_connect", type="primary"):
                with st.spinner("Connecting to IBKR Gateway…"):
                    try:
                        ok = ibkr.connect_with_retry() if ibkr else False
                        if ok:
                            if "ibkr_connected" in st.session_state:
                                st.session_state.ibkr_connected = True
                            st.rerun()
                        else:
                            st.error("❌ Could not connect. Ensure IBKR Gateway/TWS is running and API connections are enabled (port 4002 for paper, 4001 for live).")
                    except Exception as _ex:
                        st.error(f"❌ Connection error: {_ex}")
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

    loader: FinvizEliteUniverse = st.session_state.get("finviz_universe")
    dyn_enabled   = st.session_state.get("dyn_universe_enabled", False)
    finviz_tickers = list(st.session_state.get("mon_finviz_tickers", []))
    excluded       = st.session_state.get("mon_excluded_finviz", set())
    custom_tickers = list(st.session_state.get("mon_custom_tickers", []))

    # ─── Section A: Finviz Picks ──────────────────────────────────────────────
    with st.expander(
        f"🔬 Finviz Picks  {'(' + str(len([t for t in finviz_tickers if t not in excluded])) + ' active)' if finviz_tickers else '(not loaded)'}",
        expanded=True,
    ):
        if not dyn_enabled and not finviz_tickers:
            st.info("Enable **Auto-load top picks** in the Finviz panel above to populate this section.")
        elif not finviz_tickers:
            st.info("Click **🔄 Refresh Now** in the Finviz panel to load picks.")
        else:
            # Status line
            if dyn_enabled and loader and loader.last_refresh_time:
                secs = loader.seconds_until_next_refresh
                st.caption(
                    f"🤖 Dynamic mode · last synced {loader.last_refresh_time.strftime('%H:%M:%S UTC')} "
                    f"· next in {secs // 60}m {secs % 60:02d}s"
                )
            else:
                st.caption("Showing last-loaded Finviz picks. Dynamic auto-load is off.")

            # Per-ticker include/exclude checkboxes in a compact grid
            st.markdown("**Toggle picks on/off** (unchecked = excluded from Watchlist & Signal Board):")
            cols = st.columns(5)
            for i, ticker in enumerate(finviz_tickers):
                with cols[i % 5]:
                    checked = ticker not in excluded
                    new_val = st.checkbox(ticker, value=checked, key=f"fv_inc_{ticker}")
                    if new_val and ticker in excluded:
                        excluded.discard(ticker)
                        st.session_state.mon_excluded_finviz = excluded
                        st.rerun()
                    elif not new_val and ticker not in excluded:
                        excluded.add(ticker)
                        st.session_state.mon_excluded_finviz = excluded
                        st.rerun()

            re_col, clear_col = st.columns([1, 1])
            with re_col:
                if st.button("✅ Select All", key="fv_select_all"):
                    st.session_state.mon_excluded_finviz = set()
                    st.rerun()
            with clear_col:
                if st.button("☐ Deselect All", key="fv_deselect_all"):
                    st.session_state.mon_excluded_finviz = set(finviz_tickers)
                    st.rerun()

    # ─── Section B: Custom Tickers ────────────────────────────────────────────
    with st.expander(f"✏️ My Custom Tickers  ({len(custom_tickers)} symbols)", expanded=True):
        add_col, remove_col, preset_col = st.columns(3)

        with add_col:
            new_sym = st.text_input("Add ticker", key="mon_add_sym", placeholder="e.g. GOOG")
            if st.button("➕ Add", key="mon_add_btn"):
                sym_clean = new_sym.strip().upper()
                if sym_clean and sym_clean not in custom_tickers:
                    custom_tickers.append(sym_clean)
                    st.session_state.mon_custom_tickers = custom_tickers
                    st.rerun()
                elif sym_clean in custom_tickers:
                    st.warning(f"{sym_clean} is already in custom tickers.")

        with remove_col:
            if custom_tickers:
                to_remove = st.selectbox(
                    "Remove ticker", ["—"] + custom_tickers, key="mon_remove_sym"
                )
                if st.button("➖ Remove", key="mon_remove_btn") and to_remove != "—":
                    custom_tickers.remove(to_remove)
                    st.session_state.mon_custom_tickers = custom_tickers
                    st.rerun()

        with preset_col:
            preset = st.selectbox("Load preset", ["—"] + list(_UNIVERSE_GROUPS.keys()), key="mon_preset")
            if st.button("Load preset", key="mon_load_preset") and preset != "—":
                st.session_state.mon_custom_tickers = list(_UNIVERSE_GROUPS[preset])
                st.rerun()

        if custom_tickers:
            st.caption("Current custom tickers: " + "  ·  ".join(custom_tickers))
        else:
            st.caption("No custom tickers yet. Add some above.")

    # ─── Combined live table ──────────────────────────────────────────────────
    watchlist = _get_active_watchlist()
    if not watchlist:
        st.info("No active tickers. Enable Finviz picks or add custom tickers above.")
        return

    # Build a lookup for Finviz scores so we can show them inline
    scored_df: Optional[pd.DataFrame] = st.session_state.get("dyn_universe_scored_df")
    score_map: Dict[str, float] = {}
    if scored_df is not None and not scored_df.empty and "Ticker" in scored_df.columns:
        for _, row in scored_df.iterrows():
            t = row.get("Ticker", "")
            s = row.get("Scalping_Score")
            if t and s is not None:
                try:
                    score_map[str(t)] = float(s)
                except Exception:
                    pass

    with st.spinner(f"Fetching live quotes for {len(watchlist)} tickers…"):
        quotes = _fetch_quotes(watchlist)
        st.session_state.mon_quote_cache = quotes

    with st.spinner("Calculating RSI & trend signals…"):
        signals = {sym: _fetch_signal(sym) for sym in watchlist}

    triggered = _check_alerts(quotes)

    fv_set = set(t for t in finviz_tickers if t not in excluded)

    rows = []
    for sym in watchlist:
        q         = quotes.get(sym, {})
        sig       = signals.get(sym, {})
        price     = q.get("price")
        pct_chg   = q.get("pct_change")
        alert_hit = sym in triggered
        source    = "🔬 Finviz" if sym in fv_set else "✏️ Custom"
        score     = score_map.get(sym)

        rows.append({
            "🔔":          "🔔" if alert_hit else "",
            "Source":      source,
            "Symbol":      sym,
            "Score":       f"{score:.1f}" if score is not None else "—",
            "Last":        f"${price:.2f}" if price else "—",
            "Change":      f"{'+' if (q.get('change') or 0) >= 0 else ''}{q.get('change'):.2f}" if q.get("change") is not None else "—",
            "% Chg":       f"{'+' if (pct_chg or 0) >= 0 else ''}{pct_chg:.2f}%" if pct_chg is not None else "—",
            "Volume":      _vol_ratio(q.get("volume"), q.get("avg_volume")),
            "Day Hi":      f"${q['day_high']:.2f}" if q.get("day_high") else "—",
            "Day Lo":      f"${q['day_low']:.2f}" if q.get("day_low") else "—",
            "RSI(14)":     f"{sig['rsi']:.1f}" if sig.get("rsi") else "—",
            "Trend":       sig.get("trend", "—"),
            "Signal":      f"{_signal_color(sig.get('signal','—'))} {sig.get('signal','—')}",
        })

    df = pd.DataFrame(rows)
    st.caption(f"Showing **{len(df)}** tickers  ({len(fv_set)} Finviz · {len([s for s in watchlist if s not in fv_set])} Custom)")
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
    """Visual heatmap supporting both RSI-trend signals and IBKR live signals."""
    if not signals:
        return

    # Unified priority order (IBKR types + RSI types)
    order = ["STRONG_LONG", "BUY", "LONG", "WATCH LONG", "NEUTRAL",
             "WATCH SHORT", "SHORT", "SELL", "STRONG_SHORT", "—"]
    color_map = {
        "STRONG_LONG":  "#00aa33",
        "BUY":          "#00cc44",
        "LONG":         "#55bb44",
        "WATCH LONG":   "#88cc00",
        "NEUTRAL":      "#666666",
        "WATCH SHORT":  "#cc8800",
        "SHORT":        "#cc5522",
        "SELL":         "#cc2200",
        "STRONG_SHORT": "#aa0000",
        "—":            "#444444",
    }

    def _sort_key(item):
        sig_val = item[1].get("ibkr_signal") or item[1].get("signal", "—")
        return order.index(sig_val) if sig_val in order else len(order)

    rows = sorted(signals.items(), key=_sort_key)

    cols = st.columns(min(len(rows), 5))
    for i, (sym, sig) in enumerate(rows):
        ibkr_sig  = sig.get("ibkr_signal")
        rsi_sig   = sig.get("signal", "—")
        display_sig = ibkr_sig if ibkr_sig else rsi_sig
        color     = color_map.get(display_sig, "#444444")
        strength  = sig.get("ibkr_strength")
        rsi_val   = sig.get("rsi")
        sub_lines = []
        if strength is not None and strength != 0:
            sub_lines.append(f"Str: {abs(strength)}")
        if rsi_val:
            sub_lines.append(f"RSI {rsi_val:.0f}")
        sub = "  ".join(sub_lines)
        with cols[i % 5]:
            st.markdown(
                f'<div style="background:{color};color:white;border-radius:8px;'
                f'padding:10px 6px;text-align:center;margin:2px;font-size:0.8rem">'
                f'<b>{sym}</b><br>{display_sig}<br><small>{sub}</small></div>',
                unsafe_allow_html=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic Finviz Universe Panel
# ─────────────────────────────────────────────────────────────────────────────

def _render_dynamic_universe():
    """
    Downloads the complete Finviz Elite universe, scores every ticker for
    capital-preservative scalping, and surfaces the top-10 picks.
    Runs automatically every 5 minutes; can be force-refreshed anytime.
    When enabled, the top-10 replace the manual watchlist.
    """
    loader: FinvizEliteUniverse = st.session_state.finviz_universe
    ttl    = loader.cache_ttl   # 300 s

    with st.expander("🔬 Dynamic Finviz Universe  —  Top Scalping Picks", expanded=True):

        # ── Control row ───────────────────────────────────────────────────────
        ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4 = st.columns([2, 1, 1, 2])

        with ctrl_col1:
            enabled = st.toggle(
                "Auto-load top picks into watchlist (refreshes every 5 min)",
                value=st.session_state.dyn_universe_enabled,
                key="dyn_toggle",
            )
            st.session_state.dyn_universe_enabled = enabled

        with ctrl_col2:
            n_picks = st.number_input(
                "# Picks", min_value=3, max_value=30, value=10, step=1,
                key="dyn_n_picks", help="How many top tickers to select"
            )

        with ctrl_col3:
            force = st.button("🔄 Refresh Now", key="dyn_force_refresh",
                              help="Force re-download from Finviz Elite")

        with ctrl_col4:
            # Live countdown
            secs = loader.seconds_until_next_refresh
            if loader.last_refresh_time:
                last_str = loader.last_refresh_time.strftime("%H:%M:%S UTC")
                st.caption(
                    f"Last loaded: **{last_str}** · "
                    f"Next refresh in: **{secs // 60}m {secs % 60:02d}s** · "
                    f"Universe size: **{loader.universe_size:,}** securities"
                )
            else:
                st.caption("Universe not yet loaded — click **Refresh Now** or enable auto-load.")

        # ── Auto-refresh logic ────────────────────────────────────────────────
        # Trigger a fetch if: forced, or enabled + cache stale
        should_fetch = force or (enabled and loader.seconds_until_next_refresh == 0)

        if should_fetch:
            with st.spinner(
                "📡 Downloading complete Finviz Elite universe (no filters)…  "
                "This may take 15–30 seconds."
            ):
                scored_df = loader.get_scored_universe(force_refresh=True)
                if scored_df is not None and not scored_df.empty:
                    st.session_state.dyn_universe_scored_df = scored_df
                    st.session_state.dyn_universe_last_loaded = loader.last_refresh_time
                    st.success(
                        f"✅ Universe loaded: **{loader.universe_size:,}** securities "
                        f"→ scored & ranked. Top **{n_picks}** scalping picks updated."
                    )
                else:
                    st.error(
                        "❌ Failed to download Finviz universe. "
                        "Check your auth token and internet connection."
                    )
        elif not loader._cache_valid() and st.session_state.dyn_universe_scored_df is not None:
            # Use cached data from a previous successful load
            pass

        scored_df: Optional[pd.DataFrame] = st.session_state.dyn_universe_scored_df

        # ── Push top picks into mon_finviz_tickers ────────────────────────────
        if enabled and scored_df is not None and not scored_df.empty:
            top_tickers = scored_df["Ticker"].head(int(n_picks)).tolist()
            if top_tickers != st.session_state.mon_finviz_tickers:
                st.session_state.mon_finviz_tickers = top_tickers
                # Clear exclusions that are no longer in the list
                st.session_state.mon_excluded_finviz = {
                    t for t in st.session_state.mon_excluded_finviz
                    if t in top_tickers
                }

        # ── Score table ───────────────────────────────────────────────────────
        if scored_df is not None and not scored_df.empty:
            top_df = scored_df.head(int(n_picks)).copy()

            # Select and format display columns
            display_cols = ["Rank", "Ticker"]
            optional_cols = {
                "Price":           ("Price",     lambda v: f"${v:.2f}" if v else "—"),
                "Change_Pct":      ("Chg%",      lambda v: f"{v:+.2f}%" if v else "—"),
                "Rel_Volume":      ("Rel Vol",   lambda v: f"{v:.1f}x" if v else "—"),
                "Avg_Volume":      ("Avg Vol",   lambda v: f"{v/1e6:.1f}M" if v and v >= 1e6 else (f"{v/1e3:.0f}K" if v else "—")),
                "ATR_Pct":         ("ATR%",      lambda v: f"{v:.2f}%" if v else "—"),
                "Market_Cap":      ("Mkt Cap",   lambda v: f"${v/1e9:.1f}B" if v and v >= 1e9 else (f"${v/1e6:.0f}M" if v else "—")),
                "Scalping_Score":  ("Score",     lambda v: f"{v:.1f}" if v else "—"),
                "Liquidity_Score": ("Liq",       lambda v: f"{v:.0f}" if v else "—"),
                "ATR_Score":       ("ATR Sc",    lambda v: f"{v:.0f}" if v else "—"),
                "Stability_Score": ("Stab",      lambda v: f"{v:.0f}" if v else "—"),
            }

            rows = []
            for _, row in top_df.iterrows():
                r = {"Rank": int(row.get("Rank", 0)), "Ticker": row.get("Ticker", "")}
                for src_col, (label, fmt) in optional_cols.items():
                    val = row.get(src_col)
                    try:
                        r[label] = fmt(float(val)) if val is not None else "—"
                    except Exception:
                        r[label] = "—"
                rows.append(r)

            display_df = pd.DataFrame(rows)
            st.dataframe(display_df, use_container_width=True, hide_index=True)

            # Score legend
            with st.expander("📖 Score legend", expanded=False):
                st.markdown("""
| Component | Weight | What it measures |
|---|---|---|
| **Liq** (Liquidity) | 45% | Relative volume today × Average daily volume — favours actively traded, tight-spread names |
| **ATR Sc** (ATR sweet-spot) | 25% | ATR% ideally 1.5–4.5% — enough intraday range to scalp profitably without blow-up risk |
| **Stab** (Stability) | 20% | Market-cap size (larger = faster recovery) + bounded day-change (avoids news-gap bombs) |
| **Price** (Price bracket) | 10% | $20–$200 is ideal for position sizing in retail accounts |
| **Score** | Total | Weighted composite 0–100 — sort descending to find best scalps |
                """)

            # Scoring note
            st.info(
                "💡 **Capital-preservative philosophy:** These picks are liquid, "
                "move enough to be worth scalping, and are stable enough that a bad "
                "trade can be recovered within 1–2 sessions. Avoid over-sizing any "
                "single position — use ATR-based stops."
            )
        else:
            st.info(
                "Click **🔄 Refresh Now** to download the complete Finviz Elite universe "
                "and compute the top scalping picks, or enable the auto-load toggle above."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Signal Board
# ─────────────────────────────────────────────────────────────────────────────

def _render_signal_board():
    """
    Full enriched signal board with IBKR live quotes when connected,
    falling back to yfinance. Shows live bid/ask/spread/volume alongside
    RSI+trend signals and the IBKR mean-reversion signal.
    """
    ibkr      = st.session_state.get("ibkr")
    connected = st.session_state.get("ibkr_connected", False)

    st.subheader("📊 Signal Board")

    # ── IBKR quote controls ───────────────────────────────────────────────────
    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([3, 1, 1])
    with ctrl_col1:
        if connected:
            st.success("⚡ IBKR live quotes available", icon="✅")
        else:
            st.info("📊 Using yfinance (IBKR not connected)")
    with ctrl_col2:
        if connected and st.button("🔄 Refresh Quotes", key="sb_refresh_ibkr"):
            st.session_state.mon_ibkr_quotes = {}
            st.rerun()
    with ctrl_col3:
        use_ibkr = st.toggle(
            "Use IBKR Data", value=connected, key="sb_use_ibkr", disabled=not connected
        )

    watchlist = _get_active_watchlist()
    if not watchlist:
        st.info("No active tickers. Enable Finviz picks or add custom tickers in the Watchlist tab.")
        return

    # ── Finviz score lookup ───────────────────────────────────────────────────
    scored_df: Optional[pd.DataFrame] = st.session_state.get("dyn_universe_scored_df")
    score_map: Dict[str, float] = {}
    atr_pct_map: Dict[str, float] = {}
    if scored_df is not None and not scored_df.empty and "Ticker" in scored_df.columns:
        for _, row in scored_df.iterrows():
            t = str(row.get("Ticker", ""))
            if t:
                s = row.get("Scalping_Score")
                a = row.get("ATR_Pct")
                if s is not None:
                    try:
                        score_map[t] = float(s)
                    except Exception:
                        pass
                if a is not None:
                    try:
                        atr_pct_map[t] = float(a)
                    except Exception:
                        pass

    finviz_active = set(
        t for t in st.session_state.get("mon_finviz_tickers", [])
        if t not in st.session_state.get("mon_excluded_finviz", set())
    )

    # ── Fetch IBKR live quotes (cached until refresh) ─────────────────────────
    ibkr_quotes: Dict[str, Dict] = {}
    if connected and use_ibkr:
        cached = st.session_state.get("mon_ibkr_quotes", {})
        if cached:
            ibkr_quotes = cached
        else:
            with st.spinner(f"⚡ Fetching IBKR live quotes for {len(watchlist)} tickers…"):
                try:
                    iq = ibkr.get_live_quotes(watchlist, wait_secs=2.5)
                    ibkr_quotes = iq or {}
                    st.session_state.mon_ibkr_quotes = ibkr_quotes
                except Exception as e:
                    st.warning(f"IBKR quote fetch failed: {e}")

    # ── RSI / trend signals (always computed) ─────────────────────────────────
    with st.spinner(f"Computing RSI signals for {len(watchlist)} tickers…"):
        rsi_signals = {sym: _fetch_signal(sym) for sym in watchlist}

    # ── Build combined signals dict ───────────────────────────────────────────
    combined: Dict[str, Dict] = {}
    for sym in watchlist:
        rsi_sig = rsi_signals.get(sym, {})
        iq      = ibkr_quotes.get(sym, {})
        ibkr_signal_val, ibkr_strength_val = (None, None)
        if iq:
            ibkr_signal_val, ibkr_strength_val = calculate_ibkr_signal(iq)
        combined[sym] = {
            **rsi_sig,
            "ibkr_signal":   ibkr_signal_val,
            "ibkr_strength": ibkr_strength_val,
            **iq,
        }

    # ── Visual heatmap ────────────────────────────────────────────────────────
    st.markdown("#### Signal Heatmap")
    _render_signal_heatmap(combined)
    st.divider()

    # ── Signal legend ─────────────────────────────────────────────────────────
    with st.expander("📖 Signal Legend", expanded=False):
        legend_data = [
            ("STRONG_LONG",  "#00aa33", "Price at low of range, strong volume, tight spread — high-probability long"),
            ("LONG",         "#55bb44", "Price below midpoint, above-avg volume — mild long bias"),
            ("NEUTRAL",      "#666666", "No clear directional edge"),
            ("SHORT",        "#cc5522", "Price above midpoint, above-avg volume — mild short bias"),
            ("STRONG_SHORT", "#aa0000", "Price at high of range, strong volume, tight spread — high-probability short"),
            ("BUY",          "#00cc44", "RSI oversold + uptrend"),
            ("WATCH LONG",   "#88cc00", "RSI moderately low + uptrend"),
            ("WATCH SHORT",  "#cc8800", "RSI moderately high + downtrend"),
            ("SELL",         "#cc2200", "RSI overbought + downtrend"),
        ]
        for sig_name, color, desc in legend_data:
            st.markdown(
                f'<span style="background:{color};color:white;border-radius:4px;'
                f'padding:2px 8px;font-size:0.8rem;margin-right:6px">{sig_name}</span> {desc}',
                unsafe_allow_html=True,
            )

    # ── Detail table ──────────────────────────────────────────────────────────
    st.markdown("#### Signal Detail Table")

    # Sort controls
    sort_col1, sort_col2 = st.columns([2, 1])
    with sort_col1:
        sort_by = st.selectbox(
            "Sort by",
            ["Finviz Score ↓", "Signal", "RSI ↑ (oversold first)", "RSI ↓ (overbought first)", "Symbol A→Z"],
            key="sb_sort_by",
        )
    with sort_col2:
        show_source_filter = st.multiselect(
            "Show source",
            ["🔬 Finviz", "✏️ Custom"],
            default=["🔬 Finviz", "✏️ Custom"],
            key="sb_source_filter",
        )

    rows = []
    for sym in watchlist:
        sig    = combined.get(sym, {})
        source = "🔬 Finviz" if sym in finviz_active else "✏️ Custom"
        if source not in show_source_filter:
            continue

        score   = score_map.get(sym)
        atr_p   = atr_pct_map.get(sym)
        rsi     = sig.get("rsi")
        trend   = sig.get("trend", "—")
        rsi_sig = sig.get("signal", "—")
        ibkr_s  = sig.get("ibkr_signal")
        ibkr_st = sig.get("ibkr_strength")

        last    = sig.get("last")
        bid     = sig.get("bid")
        ask     = sig.get("ask")
        spread  = sig.get("spread")
        high    = sig.get("high")
        low     = sig.get("low")
        volume  = sig.get("volume")
        chg_pct = sig.get("change_pct")

        # Fall back to yfinance cache if no IBKR quote
        if last is None:
            q       = st.session_state.mon_quote_cache.get(sym, {})
            last    = q.get("price")
            chg_pct = q.get("pct_change")

        data_source = "⚡ IBKR" if sym in ibkr_quotes else "📊 yfinance"

        rows.append({
            "Source":        source,
            "Data":          data_source,
            "Symbol":        sym,
            "_score_raw":    score if score is not None else -1,
            "Score":         f"{score:.1f}" if score is not None else "—",
            "Last":          f"${last:.2f}" if last is not None else "—",
            "Bid":           f"${bid:.2f}" if bid is not None else "—",
            "Ask":           f"${ask:.2f}" if ask is not None else "—",
            "Spread":        f"${spread:.3f}" if spread is not None else "—",
            "High":          f"${high:.2f}" if high is not None else "—",
            "Low":           f"${low:.2f}" if low is not None else "—",
            "Chg%":          f"{'+' if (chg_pct or 0) >= 0 else ''}{chg_pct:.2f}%" if chg_pct is not None else "—",
            "Volume":        f"{int(volume):,}" if volume is not None else "—",
            "ATR%":          f"{atr_p:.2f}%" if atr_p is not None else "—",
            "RSI(14)":       f"{rsi:.1f}" if rsi else "—",
            "Trend":         trend,
            "Signal (IBKR)": f"{_signal_color(ibkr_s)} {ibkr_s}" if ibkr_s else "—",
            "Signal (RSI)":  f"{_signal_color(rsi_sig)} {rsi_sig}",
            "_ibkr_sig":     ibkr_s or "—",
            "_rsi_sig":      rsi_sig,
            "_rsi_raw":      rsi if rsi else 50,
        })

    if not rows:
        st.info("No tickers match the selected source filter.")
        return

    # Apply sort
    _ibkr_order = ["STRONG_LONG", "LONG", "NEUTRAL", "SHORT", "STRONG_SHORT", "—"]
    if "Score" in sort_by:
        rows.sort(key=lambda r: -(r["_score_raw"] or -1))
    elif sort_by == "Signal":
        rows.sort(key=lambda r: (
            _ibkr_order.index(r["_ibkr_sig"]) if r["_ibkr_sig"] in _ibkr_order else len(_ibkr_order)
        ))
    elif "↑" in sort_by:
        rows.sort(key=lambda r: r["_rsi_raw"])
    elif "↓" in sort_by and "RSI" in sort_by:
        rows.sort(key=lambda r: -r["_rsi_raw"])
    else:
        rows.sort(key=lambda r: r["Symbol"])

    # Build display DataFrame (drop internal sort keys)
    display_rows = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in rows
    ]
    df = pd.DataFrame(display_rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Quick-add to custom tickers ───────────────────────────────────────────
    st.divider()
    st.markdown("#### ➕ Add Ticker to Custom List")
    qa_col1, qa_col2 = st.columns([2, 1])
    with qa_col1:
        qa_sym = st.text_input(
            "Ticker symbol", key="sb_quick_add", placeholder="e.g. CRWD",
            help="Add any ticker to your custom list so it always appears here alongside Finviz picks"
        )
    with qa_col2:
        st.write("")
        st.write("")
        if st.button("➕ Add to Custom", key="sb_add_custom_btn"):
            sym_clean = qa_sym.strip().upper()
            custom = list(st.session_state.get("mon_custom_tickers", []))
            if sym_clean and sym_clean not in custom:
                custom.append(sym_clean)
                st.session_state.mon_custom_tickers = custom
                st.success(f"✅ {sym_clean} added to custom tickers.")
                st.rerun()
            elif sym_clean in custom:
                st.info(f"{sym_clean} is already in your custom list.")

    # ── Quick-remove from custom tickers ─────────────────────────────────────
    custom = list(st.session_state.get("mon_custom_tickers", []))
    if custom:
        qr_col1, qr_col2 = st.columns([2, 1])
        with qr_col1:
            to_remove = st.selectbox(
                "Remove from custom", ["—"] + custom, key="sb_quick_remove"
            )
        with qr_col2:
            st.write("")
            st.write("")
            if st.button("➖ Remove from Custom", key="sb_remove_custom_btn") and to_remove != "—":
                custom.remove(to_remove)
                st.session_state.mon_custom_tickers = custom
                st.rerun()


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

    # ── Dynamic Finviz Universe Panel (always visible) ────────────────────────
    _render_dynamic_universe()

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
        _render_signal_board()

    # ── Auto-refresh ─────────────────────────────────────────────────────────
    st.session_state.mon_last_refresh = datetime.now()

    if interval != "Off":
        seconds = int(interval.split()[0])
        loader: FinvizEliteUniverse = st.session_state.get("finviz_universe")
        fv_secs = loader.seconds_until_next_refresh if loader else 300
        fv_note = (
            f" · Finviz universe refresh in **{fv_secs // 60}m {fv_secs % 60:02d}s**"
            if st.session_state.get("dyn_universe_enabled") and loader else ""
        )
        st.caption(
            f"⏱ Page auto-refreshing every {seconds}s · "
            f"Last: {datetime.now().strftime('%H:%M:%S')}{fv_note}"
        )
        time.sleep(seconds)
        st.rerun()
