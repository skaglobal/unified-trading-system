"""
Live Dashboard — Real-time monitoring of up to 5 tickers.

Ports the trader.ai live_dashboard (Flask + SSE) experience into Streamlit:
  - IBKR live bid/ask/last streaming via get_live_quotes()
  - Mean-reversion signal detection (STRONG_LONG / LONG / NEUTRAL / SHORT / STRONG_SHORT)
  - ATR from 14-day daily bars
  - Fully configurable watchlist (max 5 tickers, add/remove any time)
  - AutoTrader integration (enable/disable, learning mode, position management)
  - PriceAnalyzer support/resistance per ticker
  - Trade history & P&L stats
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MAX_TICKERS = 8

_DEFAULT_TICKERS: list = []  # Start empty — user adds tickers manually

_PRESETS = {
    "Mag-7 (top 5)":   ["AAPL", "MSFT", "NVDA", "GOOGL", "META"],
    "Vol Leaders":     ["TSLA", "NVDA", "AMD", "PLTR", "MSTR"],
    "Indexes":         ["SPY",  "QQQ",  "IWM", "DIA",  "TLT"],
    "FAANG":           ["META", "AAPL", "AMZN", "NFLX", "GOOGL"],
    "Financials":      ["JPM",  "BAC",  "GS",   "MS",   "C"],
}

_SIGNAL_COLORS = {
    "STRONG_LONG":  "#00aa33",
    "LONG":         "#55bb44",
    "NEUTRAL":      "#666666",
    "SHORT":        "#cc5522",
    "STRONG_SHORT": "#aa0000",
}

_SIGNAL_LABEL = {
    "STRONG_LONG":  "⬆⬆ STRONG LONG",
    "LONG":         "⬆ LONG",
    "NEUTRAL":      "◼ NEUTRAL",
    "SHORT":        "⬇ SHORT",
    "STRONG_SHORT": "⬇⬇ STRONG SHORT",
}


# ─────────────────────────────────────────────────────────────────────────────
# Signal calculation (ported from dashboard.py TickerMonitor)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_signal(data: Dict) -> tuple:
    """
    Mean-reversion signal from live tick data.

    Returns (signal_str, strength_int) where:
      signal_str  : STRONG_LONG | LONG | NEUTRAL | SHORT | STRONG_SHORT
      strength_int: negative = long bias, positive = short bias (-100 to +100)
    """
    last = data.get("last") or 0
    high = data.get("high") or 0
    low  = data.get("low")  or 0
    if not last or not high or not low:
        return "NEUTRAL", 0

    price_range = high - low
    if price_range == 0:
        return "NEUTRAL", 0

    price_position = (last - low) / price_range

    long_score  = 0
    short_score = 0

    # Price position: bottom 15% = strong long; top 15% = strong short
    if price_position < 0.15:
        long_score += 35
    elif price_position < 0.25:
        long_score += 25
    elif price_position < 0.35:
        long_score += 15

    if price_position > 0.85:
        short_score += 35
    elif price_position > 0.75:
        short_score += 25
    elif price_position > 0.65:
        short_score += 15

    # Momentum / change%
    chg = data.get("change_pct") or 0
    if chg < -2.5:
        long_score += 30
    elif chg < -1.5:
        long_score += 20
    elif chg < -0.5:
        long_score += 10

    if chg > 2.5:
        short_score += 30
    elif chg > 1.5:
        short_score += 20
    elif chg > 0.5:
        short_score += 10

    # Spread / liquidity bonus
    bid    = data.get("bid")    or 0
    ask    = data.get("ask")    or 0
    liq    = 0
    if bid and ask and last:
        spread_pct = (ask - bid) / last * 100
        if spread_pct < 0.1:
            liq = 20
        elif spread_pct < 0.2:
            liq = 10

    # Volume bonus
    vol    = data.get("volume") or 0
    vol_b  = 0
    if vol > 1_000_000:
        vol_b = 15
    elif vol > 500_000:
        vol_b = 8

    long_score  += liq + vol_b
    short_score += liq + vol_b

    if long_score > short_score and long_score >= 60:
        return "STRONG_LONG",  -long_score
    if long_score > short_score and long_score >= 40:
        return "LONG",         -long_score
    if short_score > long_score and short_score >= 60:
        return "STRONG_SHORT",  short_score
    if short_score > long_score and short_score >= 40:
        return "SHORT",         short_score
    return "NEUTRAL", 0


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────

def _init_state():
    defaults = {
        "ld_tickers":          [],  # Always start empty — user adds manually
        "ld_quotes":           {},          # sym -> ibkr quote dict
        "ld_atr":              {},          # sym -> atr float
        "ld_last_refresh":     None,
        "ld_refresh_interval": 5,
        "ld_auto_refresh":     False,
        "ld_auto_trader":      None,
        "ld_price_analyzer":   None,
        "ld_trading_enabled":  False,
        "ld_learning_mode":    True,
        "ld_use_ibkr_data":    True,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _get_or_create_auto_trader(ibkr):
    """Lazily create AutoTrader bound to current IBKR connector."""
    if st.session_state.ld_auto_trader is None:
        from execution.auto_trader import AutoTrader
        st.session_state.ld_auto_trader = AutoTrader(
            ibkr=ibkr,
            allocation_per_ticker=25_000.0,
            stop_loss_pct=2.5,
            take_profit_pct=1.5,
            trade_history_file=Path("data/ld_trade_history.json"),
            min_confidence=60.0,
            analyzer=st.session_state.get("ld_price_analyzer"),
        )
    return st.session_state.ld_auto_trader


def _get_or_create_analyzer(ibkr):
    """Lazily create PriceAnalyzer."""
    if st.session_state.ld_price_analyzer is None and ibkr and ibkr.is_connected():
        from analysis.price_analyzer import PriceAnalyzer
        st.session_state.ld_price_analyzer = PriceAnalyzer(
            ibkr=ibkr,
            lookback_days=20,
            min_confidence_threshold=60.0,
            cache_dir=Path("analysis_cache"),
        )
    return st.session_state.ld_price_analyzer


# ─────────────────────────────────────────────────────────────────────────────
# Ticker management sidebar
# ─────────────────────────────────────────────────────────────────────────────

def _render_ticker_sidebar():
    """Render ticker add/remove controls in the sidebar section."""
    st.sidebar.markdown("---")
    st.sidebar.subheader("🖥️ Live Dashboard Tickers")
    tickers: List[str] = st.session_state.ld_tickers

    # Current list with remove buttons
    st.sidebar.caption(f"Active: {len(tickers)} / {MAX_TICKERS} — add up to {MAX_TICKERS} tickers")
    for sym in list(tickers):
        c1, c2 = st.sidebar.columns([3, 1])
        c1.markdown(f"**{sym}**")
        if c2.button("✕", key=f"ld_rm_{sym}", help=f"Remove {sym}"):
            tickers.remove(sym)
            st.session_state.ld_tickers = tickers
            # Purge stale quote/atr cache
            st.session_state.ld_quotes.pop(sym, None)
            st.session_state.ld_atr.pop(sym, None)
            st.rerun()

    # Add ticker
    if len(tickers) < MAX_TICKERS:
        new_sym = st.sidebar.text_input(
            "Add ticker", key="ld_add_input",
            placeholder="e.g. CRWD",
            label_visibility="collapsed",
        )
        if st.sidebar.button("➕ Add", key="ld_add_btn", use_container_width=True):
            sym_clean = new_sym.strip().upper()
            if sym_clean and sym_clean not in tickers:
                tickers.append(sym_clean)
                st.session_state.ld_tickers = tickers
                st.rerun()
            elif sym_clean in tickers:
                st.sidebar.warning(f"{sym_clean} already in list")
    else:
        st.sidebar.info(f"Max {MAX_TICKERS} tickers reached. Remove one to add another.")

    # Preset buttons
    st.sidebar.markdown("**Load preset:**")
    for preset_name, preset_tickers in _PRESETS.items():
        if st.sidebar.button(preset_name, key=f"ld_preset_{preset_name}", use_container_width=True):
            st.session_state.ld_tickers = list(preset_tickers[:MAX_TICKERS])
            st.session_state.ld_quotes = {}
            st.session_state.ld_atr    = {}
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# ATR helper
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_atr(ibkr, ticker: str, period: int = 14) -> Optional[float]:
    """Fetch 14-day daily bars and compute Wilder ATR."""
    try:
        df = ibkr.fetch_historical_data(
            symbol=ticker,
            duration="30 D",
            bar_size="1 day",
            what_to_show="TRADES",
            use_rth=True,
        )
        if df is None or df.empty or len(df) < period:
            return None
        # IBKR returns capitalized columns: Open, High, Low, Close, Volume, Symbol
        col_map = {c.lower(): c for c in df.columns}
        high_c  = col_map.get("high",  "High")
        low_c   = col_map.get("low",   "Low")
        close_c = col_map.get("close", "Close")
        tr_vals = []
        for i in range(len(df)):
            hl = float(df.iloc[i][high_c]) - float(df.iloc[i][low_c])
            if i == 0:
                tr_vals.append(hl)
            else:
                hc = abs(float(df.iloc[i][high_c]) - float(df.iloc[i - 1][close_c]))
                lc = abs(float(df.iloc[i][low_c])  - float(df.iloc[i - 1][close_c]))
                tr_vals.append(max(hl, hc, lc))
        atr = sum(tr_vals[-period:]) / period
        return round(atr, 2)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Ticker quote card
# ─────────────────────────────────────────────────────────────────────────────

def _ticker_card(sym: str, quote: Dict, atr: Optional[float], position: Optional[Dict]):
    """Render an enhanced, colour-rich ticker card."""
    signal, strength = calculate_signal(quote) if quote else ("NEUTRAL", 0)
    sig_color = _SIGNAL_COLORS.get(signal, "#444444")
    label     = _SIGNAL_LABEL.get(signal, signal)

    last   = quote.get("last")
    bid    = quote.get("bid")
    ask    = quote.get("ask")
    high   = quote.get("high")
    low    = quote.get("low")
    vol    = quote.get("volume")
    chg    = quote.get("change_pct")
    spread = quote.get("spread")

    # ── Card background: green-tinted when up, red-tinted when down ──────────
    chg_val = chg if chg is not None else 0.0
    if chg_val >= 2.0:
        bg1, bg2 = "#0a3320", "#062415"
    elif chg_val >= 0.0:
        bg1, bg2 = "#081f14", "#061510"
    elif chg_val >= -2.0:
        bg1, bg2 = "#200808", "#150606"
    else:
        bg1, bg2 = "#330a0a", "#240606"

    # Change % display
    chg_color = "#44ff88" if chg_val >= 0 else "#ff4444"
    chg_str   = f"{'+' if chg_val >= 0 else ''}{chg_val:.2f}%" if chg is not None else "—"

    # ── Spread: color = liquidity quality ────────────────────────────────────
    spread_color = "#aaaaaa"
    spread_str   = "—"
    if spread is not None and last:
        sp_pct = spread / last * 100
        spread_str = f"${spread:.3f}"
        if sp_pct < 0.05:
            spread_color = "#44ff88"   # very tight — excellent
        elif sp_pct < 0.15:
            spread_color = "#ccff44"   # tight — good
        elif sp_pct < 0.30:
            spread_color = "#ffaa00"   # moderate — caution
        else:
            spread_color = "#ff4444"   # wide — poor liquidity

    # ── ATR: color = volatility level ────────────────────────────────────────
    atr_color = "#aaaaaa"
    atr_str   = "—"
    if atr is not None and last:
        atr_pct_val = atr / last * 100
        atr_str = f"${atr:.2f} ({atr_pct_val:.1f}%)"
        if atr_pct_val < 1.5:
            atr_color = "#44ff88"   # low vol
        elif atr_pct_val < 3.0:
            atr_color = "#ccff44"   # moderate
        elif atr_pct_val < 5.0:
            atr_color = "#ffaa00"   # high vol
        else:
            atr_color = "#ff4444"   # very high vol

    # ── Volume: color = liquidity depth ──────────────────────────────────────
    vol_color = "#aaaaaa"
    vol_str   = "—"
    if vol is not None:
        vol_str = f"{int(vol):,}"
        if vol >= 2_000_000:
            vol_color = "#44ff88"
        elif vol >= 500_000:
            vol_color = "#ccff44"
        elif vol >= 100_000:
            vol_color = "#ffaa00"
        else:
            vol_color = "#ff4444"

    # ── Best entry rates: lit up when signal agrees, dimmed when not ─────────
    # Best for Long  = bid  (you BUY at bid price)
    # Best for Short = ask  (you SELL at ask price)
    long_active  = signal in ("LONG", "STRONG_LONG")
    short_active = signal in ("SHORT", "STRONG_SHORT")
    long_entry_color  = "#44ff88" if long_active  else "#555566"
    short_entry_color = "#ff4444" if short_active else "#555566"
    long_entry_glow  = f"0 0 8px #44ff8866" if long_active  else "none"
    short_entry_glow = f"0 0 8px #ff444466" if short_active else "none"
    long_entry_str  = f"${bid:.2f}"  if bid  is not None else "—"
    short_entry_str = f"${ask:.2f}" if ask is not None else "—"

    # ── Position badge ────────────────────────────────────────────────────────
    pos_badge = ""
    if position:
        direction = position.get("direction", "")
        pnl       = position.get("pnl", 0)
        pnl_color = "#44ff88" if pnl >= 0 else "#ff4444"
        pos_badge = (
            f'<div style="font-size:0.8rem;background:rgba(0,0,0,0.35);border-radius:6px;'
            f'padding:5px 8px;margin-top:8px;color:{pnl_color};font-weight:bold">'
            f'📌 {direction} &nbsp;|&nbsp; P&L ${pnl:+.2f}</div>'
        )

    card_html = f"""
<div style="
  background: linear-gradient(160deg, {bg1} 0%, {bg2} 100%);
  border: 2px solid {sig_color};
  border-radius: 14px;
  padding: 20px 16px;
  margin: 6px 4px;
  text-align: center;
  font-family: 'Courier New', monospace;
  box-shadow: 0 4px 24px rgba(0,0,0,0.6), 0 0 16px {sig_color}44;
">
  <div style="font-size:1.7rem;font-weight:bold;color:#ffffff;letter-spacing:3px;
              text-shadow:0 0 12px rgba(255,255,255,0.25)">{sym}</div>
  <div style="font-size:2.4rem;font-weight:bold;color:#00d4ff;margin:8px 0;
              text-shadow:0 0 18px #00d4ff99">
    {'${:.2f}'.format(last) if last is not None else '—'}
  </div>
  <div style="font-size:1.15rem;font-weight:bold;color:{chg_color};margin-bottom:10px;letter-spacing:1px">{chg_str}</div>
  <div style="background:{sig_color};color:white;border-radius:8px;
              padding:7px 10px;margin:8px 0;font-weight:bold;font-size:1rem;
              letter-spacing:1px;box-shadow:0 2px 10px {sig_color}88">{label}</div>

  <table style="width:100%;border-collapse:separate;border-spacing:4px;margin-top:10px;text-align:left">
    <tr>
      <td style="width:50%;background:rgba(0,0,0,0.28);border-radius:8px;padding:7px 10px;
                 border:1px solid {long_entry_color}44">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">&#128994; Best for Long</div>
        <div style="color:{long_entry_color};font-size:1.05rem;font-weight:bold">{long_entry_str}</div>
      </td>
      <td style="width:50%;background:rgba(0,0,0,0.28);border-radius:8px;padding:7px 10px;
                 border:1px solid {short_entry_color}44">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">&#128308; Best for Short</div>
        <div style="color:{short_entry_color};font-size:1.05rem;font-weight:bold">{short_entry_str}</div>
      </td>
    </tr>
    <tr>
      <td style="background:rgba(0,0,0,0.22);border-radius:8px;padding:7px 10px">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Day High</div>
        <div style="color:#44ff88;font-size:1.0rem;font-weight:bold">{'${:.2f}'.format(high) if high is not None else '&#8212;'}</div>
      </td>
      <td style="background:rgba(0,0,0,0.22);border-radius:8px;padding:7px 10px">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Day Low</div>
        <div style="color:#ff4444;font-size:1.0rem;font-weight:bold">{'${:.2f}'.format(low) if low is not None else '&#8212;'}</div>
      </td>
    </tr>
    <tr>
      <td style="background:rgba(0,0,0,0.22);border-radius:8px;padding:7px 10px">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Spread</div>
        <div style="color:{spread_color};font-size:1.0rem;font-weight:bold">{spread_str}</div>
      </td>
      <td style="background:rgba(0,0,0,0.22);border-radius:8px;padding:7px 10px">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">ATR</div>
        <div style="color:{atr_color};font-size:0.85rem;font-weight:bold">{atr_str}</div>
      </td>
    </tr>
    <tr>
      <td colspan="2" style="background:rgba(0,0,0,0.22);border-radius:8px;padding:7px 10px">
        <div style="color:#888;font-size:0.6rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Volume</div>
        <div style="color:{vol_color};font-size:1.0rem;font-weight:bold">{vol_str}</div>
      </td>
    </tr>
  </table>

  {pos_badge}
</div>
"""
    st.markdown(card_html, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Signal heatmap row
# ─────────────────────────────────────────────────────────────────────────────

def _render_heatmap(tickers: List[str], quotes: Dict, positions: Dict):
    """Compact colour-coded heatmap strip for all tickers."""
    if not tickers:
        return
    cols = st.columns(len(tickers))
    for i, sym in enumerate(tickers):
        q = quotes.get(sym, {})
        signal, _ = calculate_signal(q) if q else ("NEUTRAL", 0)
        color  = _SIGNAL_COLORS.get(signal, "#444444")
        last   = q.get("last")
        chg    = q.get("change_pct")
        chg_s  = f"{chg:+.2f}%" if chg is not None else "—"
        pos    = "📌" if sym in positions else ""
        with cols[i]:
            st.markdown(
                f'<div style="background:{color};color:white;border-radius:8px;'
                f'padding:8px 4px;text-align:center;font-size:0.78rem;margin:2px">'
                f'<b>{sym}</b> {pos}<br>'
                f'{"${:.2f}".format(last) if last else "—"}<br>'
                f'<small>{chg_s}</small></div>',
                unsafe_allow_html=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Auto-Trader tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_auto_trader_tab(ibkr, quotes: Dict):
    connected = st.session_state.get("ibkr_connected", False)
    trader    = _get_or_create_auto_trader(ibkr) if ibkr else None

    st.subheader("🤖 Auto Trader")

    if not connected:
        st.warning("⚠️ IBKR not connected — Auto Trader requires a live IBKR connection.")
        return

    if trader is None:
        st.error("Could not initialise AutoTrader.")
        return

    # ── Controls ──────────────────────────────────────────────────────────────
    ctrl1, ctrl2, ctrl3 = st.columns(3)
    with ctrl1:
        if trader.auto_trading_enabled:
            if st.button("⏹ Disable Auto Trading", use_container_width=True):
                trader.disable_trading()
                st.session_state.ld_trading_enabled = False
                st.rerun()
        else:
            if st.button("▶️ Enable Auto Trading", type="primary", use_container_width=True):
                trader.enable_trading(st.session_state.ld_tickers)
                st.session_state.ld_trading_enabled = True
                st.rerun()

    with ctrl2:
        if trader.learning_mode:
            if st.button("📈 Go Live (disable learning)", use_container_width=True):
                trader.disable_learning_mode()
                st.rerun()
            st.info("🎓 Learning Mode ON — observing, no orders sent")
        else:
            if st.button("🎓 Enable Learning Mode", use_container_width=True):
                trader.enable_learning_mode()
                st.rerun()
            st.success("🔴 Live Trading — orders will be placed")

    with ctrl3:
        if trader.active_positions:
            if st.button("🛑 Close All Positions", use_container_width=True):
                current_prices = {s: (q.get("last") or 0) for s, q in quotes.items()}
                trader.close_all_positions(current_prices)
                st.success("All positions closed")
                st.rerun()

    st.divider()

    # ── Configuration ─────────────────────────────────────────────────────────
    with st.expander("⚙️ Trade Parameters", expanded=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            alloc = st.number_input("Allocation / ticker ($)", 1000, 1_000_000,
                                    int(trader.allocation), 1000, key="ld_alloc")
        with c2:
            sl_pct = st.number_input("Stop Loss %", 0.5, 10.0,
                                     trader.sl_pct * 100, 0.25, key="ld_sl")
        with c3:
            tp_pct = st.number_input("Take Profit %", 0.25, 10.0,
                                     trader.tp_pct * 100, 0.25, key="ld_tp")
        if st.button("💾 Apply Parameters", key="ld_apply_params"):
            trader.allocation = float(alloc)
            trader.sl_pct     = sl_pct / 100.0
            trader.tp_pct     = tp_pct / 100.0
            st.success("Parameters updated")

    # ── Active positions ──────────────────────────────────────────────────────
    st.subheader("📂 Active Positions")
    active = trader.get_active_positions()
    if active:
        pos_df = pd.DataFrame(active)[
            ["ticker", "direction", "quantity", "entry_price",
             "stop_loss", "take_profit", "pnl", "pnl_percent", "entry_time"]
        ]
        pos_df.columns = [
            "Ticker", "Dir", "Qty", "Entry $", "SL $", "TP $",
            "P&L $", "P&L %", "Entry Time"
        ]
        st.dataframe(pos_df, use_container_width=True, hide_index=True)
    else:
        st.info("No active positions")

    # ── Stats ─────────────────────────────────────────────────────────────────
    st.subheader("📊 Session Statistics")
    stats = trader.get_trading_stats()
    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
    sc1.metric("Total Trades",  stats["total_trades"])
    sc2.metric("Win Rate",      f"{stats['win_rate']:.1f}%")
    sc3.metric("Total P&L",     f"${stats['total_pnl']:+,.2f}")
    sc4.metric("Avg P&L",       f"${stats['avg_pnl']:+,.2f}")
    sc5.metric("Best Trade",    f"${stats['best_trade']:+,.2f}")


# ─────────────────────────────────────────────────────────────────────────────
# Trade history tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_trade_history_tab(ibkr):
    st.subheader("📜 Trade History")
    trader = st.session_state.get("ld_auto_trader")
    if trader is None:
        st.info("No trades yet — AutoTrader has not been initialised.")
        return
    history = trader.get_trade_history(limit=100)
    if not history:
        st.info("No closed trades yet.")
        return
    df = pd.DataFrame(history)[
        ["ticker", "direction", "quantity", "entry_price", "exit_price",
         "pnl", "pnl_percent", "exit_reason", "entry_time", "exit_time"]
    ]
    df.columns = [
        "Ticker", "Dir", "Qty", "Entry $", "Exit $",
        "P&L $", "P&L %", "Reason", "Entry Time", "Exit Time"
    ]
    st.dataframe(df, use_container_width=True, hide_index=True)

    totals = trader.get_trading_stats()
    if totals["total_trades"] > 0:
        st.markdown(
            f"**{totals['total_trades']} trades** | "
            f"Win rate **{totals['win_rate']:.1f}%** | "
            f"Total P&L **${totals['total_pnl']:+,.2f}** | "
            f"Best **${totals['best_trade']:+.2f}** | "
            f"Worst **${totals['worst_trade']:+.2f}**"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Price analysis tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_analysis_tab(ibkr, quotes: Dict):
    st.subheader("🔬 Price Analysis (Support / Resistance)")
    connected = st.session_state.get("ibkr_connected", False)
    if not connected:
        st.warning("⚠️ IBKR connection required for historical data analysis.")
        return

    analyzer = _get_or_create_analyzer(ibkr)
    if analyzer is None:
        st.error("PriceAnalyzer could not be initialised — check IBKR connection.")
        return

    tickers = st.session_state.ld_tickers
    selected = st.selectbox("Select ticker to analyse", tickers, key="ld_analysis_ticker")

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("🔍 Run Analysis", key="ld_run_analysis", type="primary"):
            price = (quotes.get(selected) or {}).get("last") or 0.0
            with st.spinner(f"Analysing {selected} — 20 days of 1-min bars…"):
                analysis = analyzer.analyze_ticker(selected, price)
            st.session_state[f"ld_analysis_{selected}"] = analysis

    cached = st.session_state.get(f"ld_analysis_{selected}")
    if cached is None:
        st.info("Click 'Run Analysis' to fetch historical data and identify S/R levels.")
        return

    a = cached
    # Header
    rec_color = {"LONG": "#00aa33", "SHORT": "#cc5522", "WAIT": "#cc8800", "AVOID": "#666666"}
    col = rec_color.get(a.recommendation, "#666666")
    st.markdown(
        f'<div style="background:{col};color:white;border-radius:8px;'
        f'padding:10px 14px;margin-bottom:12px">'
        f'<b>{a.ticker}</b> — <b>{a.recommendation}</b><br>'
        f'<small>{a.reason}</small></div>',
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Long Confidence",  f"{a.long_confidence:.0f}%")
    m2.metric("Short Confidence", f"{a.short_confidence:.0f}%")
    m3.metric("Nearest Support",
              f"${a.nearest_support:.2f}" if a.nearest_support else "—",
              delta=f"{a.distance_to_support_pct:.1f}% away" if a.nearest_support else None)
    m4.metric("Nearest Resistance",
              f"${a.nearest_resistance:.2f}" if a.nearest_resistance else "—",
              delta=f"{a.distance_to_resistance_pct:.1f}% away" if a.nearest_resistance else None)

    # Support / Resistance tables
    col_sup, col_res = st.columns(2)
    with col_sup:
        st.markdown("**🟢 Support Levels**")
        if a.support_levels:
            rows = [{"Price": f"${l.price:.2f}", "Touches": l.touches,
                     "Bounces": l.bounces, "Success%": f"{l.success_rate*100:.0f}%",
                     "Strength": f"{l.strength:.0f}"}
                    for l in a.support_levels]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("No support levels identified")

    with col_res:
        st.markdown("**🔴 Resistance Levels**")
        if a.resistance_levels:
            rows = [{"Price": f"${l.price:.2f}", "Touches": l.touches,
                     "Bounces": l.bounces, "Success%": f"{l.success_rate*100:.0f}%",
                     "Strength": f"{l.strength:.0f}"}
                    for l in a.resistance_levels]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("No resistance levels identified")

    # Patterns
    if a.patterns:
        st.markdown("**📐 Trading Patterns**")
        pat_rows = [
            {
                "Level $":     f"${p.price_level:.2f}",
                "Pattern":     p.pattern_type,
                "Occurrences": p.occurrences,
                "Win Rate":    f"{p.win_rate*100:.0f}%",
                "Avg Gain%":   f"{p.avg_gain_pct:.2f}%",
                "Avg Loss%":   f"{p.avg_loss_pct:.2f}%",
                "Avg Min":     p.avg_duration_minutes,
                "Confidence":  f"{p.confidence:.0f}%",
            }
            for p in a.patterns
        ]
        st.dataframe(pd.DataFrame(pat_rows), hide_index=True, use_container_width=True)

    st.caption(
        f"Analysis date: {a.analysis_date[:19]} | "
        f"{a.days_analyzed}d lookback | {a.data_points} data points"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main render
# ─────────────────────────────────────────────────────────────────────────────

def render():
    _init_state()

    ibkr      = st.session_state.get("ibkr")
    # Always use live check — session_state.ibkr_connected can be stale
    connected = bool(ibkr and ibkr.is_connected())
    st.session_state.ibkr_connected = connected

    # Sidebar ticker management
    _render_ticker_sidebar()

    tickers: List[str] = st.session_state.ld_tickers

    # ── Header ────────────────────────────────────────────────────────────────
    st.title("🖥️ Live Dashboard")
    st.caption(
        "Real-time IBKR streaming for up to 8 tickers — mean-reversion signals, "
        "bid/ask/spread, ATR, Best for Long/Short, automated trading."
    )

    # ── Connection & refresh bar ──────────────────────────────────────────────
    hdr1, hdr2, hdr3, hdr4 = st.columns([2, 1, 1, 1])
    with hdr1:
        if connected:
            st.success("⚡ IBKR connected — live quotes active")
        else:
            st.warning("📊 IBKR not connected — quotes unavailable")

    with hdr2:
        interval = st.selectbox(
            "Refresh", ["Off", "3s", "5s", "10s", "30s"],
            index=1, key="ld_interval_sel",
            label_visibility="collapsed",
        )
        st.session_state.ld_auto_refresh = interval != "Off"
        if interval != "Off":
            st.session_state.ld_refresh_interval = int(interval.rstrip("s"))

    with hdr3:
        if st.button("🔄 Refresh Now", key="ld_refresh_btn", use_container_width=True):
            # Force re-subscription by clearing sub state on connector
            if ibkr and hasattr(ibkr, "_live_subs"):
                ibkr._live_subs.clear()
                ibkr._live_contracts.clear()
            st.session_state.ld_quotes = {}
            st.session_state.ld_atr = {}  # clear cached ATR so it recomputes correctly

    with hdr4:
        use_ibkr = st.toggle(
            "IBKR Data", value=connected, key="ld_use_ibkr_toggle",
            disabled=not connected,
        )
        st.session_state.ld_use_ibkr_data = use_ibkr and connected

    st.divider()

    if not tickers:
        st.info("No tickers yet. Use the sidebar to add up to 8 tickers →")
        return

    # ── Fetch live quotes ─────────────────────────────────────────────────────
    # With persistent subscriptions in the connector, subsequent calls are
    # instant (no sleep). Only the very first call for each new ticker waits.
    quotes: Dict = st.session_state.ld_quotes
    use_ibkr = st.session_state.ld_use_ibkr_data and connected and ibkr
    if use_ibkr:
        first_fetch = not quotes  # True when no data yet
        wait_secs   = 3.0 if first_fetch else 0.0
        if first_fetch:
            spinner_text = f"⚡ Subscribing to live quotes for {len(tickers)} tickers…"
        else:
            spinner_text = "⚡ Updating quotes…"
        try:
            # Only show spinner on first fetch (subsequent are near-instant)
            if first_fetch:
                with st.spinner(spinner_text):
                    fetched = ibkr.get_live_quotes(tickers, wait_secs=wait_secs) or {}
            else:
                fetched = ibkr.get_live_quotes(tickers, wait_secs=wait_secs) or {}
            st.session_state.ld_quotes = fetched
            st.session_state.ld_last_refresh = datetime.now()
            quotes = fetched
        except Exception as exc:
            st.error(f"Quote fetch error: {exc}")

    # ── ATR prefetch (low-priority, only if connected) ────────────────────────
    if connected and ibkr:
        for sym in tickers:
            if sym not in st.session_state.ld_atr:
                try:
                    atr_val = _fetch_atr(ibkr, sym)
                    st.session_state.ld_atr[sym] = atr_val
                except Exception:
                    st.session_state.ld_atr[sym] = None

    atr_map: Dict = st.session_state.ld_atr

    # Active positions from auto-trader
    trader   = st.session_state.get("ld_auto_trader")
    pos_map  = {}
    if trader:
        for pos in trader.get_active_positions():
            pos_map[pos["ticker"]] = pos

    # ── Ticker grid: up to 2 rows × 4 columns ────────────────────────────────
    COLS = 4
    row1 = tickers[:COLS]
    row2 = tickers[COLS:]

    grid1 = st.columns(len(row1))
    for i, sym in enumerate(row1):
        with grid1[i]:
            _ticker_card(sym, quotes.get(sym, {}), atr_map.get(sym), pos_map.get(sym))

    if row2:
        st.markdown("<div style='margin-top:4px'></div>", unsafe_allow_html=True)
        grid2 = st.columns(COLS)
        for i, sym in enumerate(row2):
            with grid2[i]:
                _ticker_card(sym, quotes.get(sym, {}), atr_map.get(sym), pos_map.get(sym))

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_signals, tab_auto, tab_history, tab_analysis = st.tabs(
        ["📋 Signal Detail", "🤖 Auto Trader", "📜 Trade History", "🔬 Price Analysis"]
    )

    with tab_signals:
        _render_signal_detail(tickers, quotes, atr_map, pos_map)

    with tab_auto:
        _render_auto_trader_tab(ibkr, quotes)

    with tab_history:
        _render_trade_history_tab(ibkr)

    with tab_analysis:
        _render_analysis_tab(ibkr, quotes)

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    last = st.session_state.ld_last_refresh
    last_str = last.strftime("%H:%M:%S") if last else "—"
    st.caption(f"Last refresh: {last_str}")

    if st.session_state.ld_auto_refresh and connected:
        secs = st.session_state.ld_refresh_interval
        st.caption(f"⏱ Auto-refreshing every {secs}s")
        time.sleep(secs)
        # Don't clear quotes — persistent subscriptions stream continuously;
        # the next render will call get_live_quotes(wait_secs=0) instantly.
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Signal detail table
# ─────────────────────────────────────────────────────────────────────────────

def _render_signal_detail(
    tickers: List[str],
    quotes: Dict,
    atr_map: Dict,
    pos_map: Dict,
):
    """Sortable table with all signal fields."""
    st.subheader("📋 Signal Detail")

    if not quotes:
        st.info("No quote data yet. Click 'Refresh Now' or wait for auto-refresh.")
        return

    rows = []
    for sym in tickers:
        q      = quotes.get(sym, {})
        last   = q.get("last")
        bid    = q.get("bid")
        ask    = q.get("ask")
        high   = q.get("high")
        low    = q.get("low")
        vol    = q.get("volume")
        chg    = q.get("change_pct")
        spread = q.get("spread")
        atr    = atr_map.get(sym)

        signal, strength = calculate_signal(q) if q else ("NEUTRAL", 0)
        sig_color = _SIGNAL_COLORS.get(signal, "#444444")

        pos    = pos_map.get(sym)
        pos_str = f"{pos['direction']} ${pos['pnl']:+.2f}" if pos else "—"

        rows.append({
            "Symbol":    sym,
            "Last $":    f"${last:.2f}"  if last   is not None else "—",
            "Bid $":     f"${bid:.2f}"   if bid    is not None else "—",
            "Ask $":     f"${ask:.2f}"   if ask    is not None else "—",
            "Spread $":  f"${spread:.3f}" if spread is not None else "—",
            "High $":    f"${high:.2f}"  if high   is not None else "—",
            "Low $":     f"${low:.2f}"   if low    is not None else "—",
            "Chg%":      f"{chg:+.2f}%"  if chg    is not None else "—",
            "Volume":    f"{int(vol):,}"  if vol    is not None else "—",
            "ATR $":     f"${atr:.2f}"   if atr    is not None else "—",
            "ATR%":      f"{atr/last*100:.2f}%" if (atr and last) else "—",
            "Signal":    signal,
            "Strength":  abs(strength),
            "Position":  pos_str,
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Legend
    with st.expander("📖 Signal Legend"):
        for sig, col in _SIGNAL_COLORS.items():
            label = _SIGNAL_LABEL.get(sig, sig)
            descriptions = {
                "STRONG_LONG":  "Price in bottom 15% of day range + strong volume + tight spread",
                "LONG":         "Price in bottom 25–35% of day range + moderate downward momentum",
                "NEUTRAL":      "No clear directional edge",
                "SHORT":        "Price in top 25–35% of day range + moderate upward momentum",
                "STRONG_SHORT": "Price in top 15% of day range + strong volume + tight spread",
            }
            st.markdown(
                f'<span style="background:{col};color:white;border-radius:4px;'
                f'padding:2px 10px;font-size:0.8rem;margin-right:6px">{label}</span>'
                f'{descriptions.get(sig, "")}',
                unsafe_allow_html=True,
            )
