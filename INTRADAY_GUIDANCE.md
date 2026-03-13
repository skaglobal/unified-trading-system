# Intraday Trading Guidance — Documentation

> **Disclaimer:** All output from this system is *probability guidance*, not guaranteed prediction.
> This tool is for **educational decision support only** — not automated execution.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Module Reference](#module-reference)
4. [Scoring Logic](#scoring-logic)
5. [Indicator Reference](#indicator-reference)
6. [Configuration](#configuration)
7. [Streamlit UI](#streamlit-ui)
8. [Backtesting & Paper-Trade Log](#backtesting--paper-trade-log)
9. [Running Locally](#running-locally)
10. [Limitations & Improvement Roadmap](#limitations--improvement-roadmap)

---

## Overview

The **Intraday Trading Guidance** feature adds a dedicated sidebar page to the Unified Trading System that delivers real-time, per-ticker decision support powered exclusively by **IBKR live market data**.

Key capabilities:

| Capability | Detail |
|---|---|
| Data source | IBKR Gateway / TWS via `ib_insync` (no other provider needed) |
| Timeframe | 1-minute OHLCV bars + streaming L1 tick data |
| Refresh rate | Every 5 seconds (auto-rerun via Streamlit) |
| Order book | Level-2 depth if IBKR subscription available |
| Output | Long/Short probability (0–100), confidence label, entry/stop/targets, alerts |
| Logging | CSV paper-trade event log (always on) |
| Simulation | Historical 1-min bar backtest replay |

---

## Architecture

```
streamlit_app.py
    └── views/intraday_guidance.py          ← Streamlit UI page

intraday/
    ├── __init__.py
    ├── market_data_provider.py             ← Abstract adapter + IBKR impl
    ├── indicator_engine.py                 ← All indicator computations
    ├── scoring_engine.py                   ← Weighted bull/bear scorer
    ├── guidance_engine.py                  ← Entry / stop / target / R-R
    ├── alert_engine.py                     ← Alert generation & history
    ├── paper_trade_logger.py               ← CSV event logger
    └── backtest_mode.py                    ← Historical bar replay

config/
    └── intraday_scoring.yaml               ← Factor weights & thresholds

logs/
    └── paper_trades/
        └── paper_trades_YYYYMMDD.csv       ← Daily signal log
```

### Data flow per refresh cycle

```
IBKR Gateway
    │
    ├─ reqMktData (streaming L1) ──► QuoteData (bid/ask/sizes/last/vol)
    └─ reqHistoricalData (1-min)  ──► OHLCV DataFrame
                │
                ▼
        IndicatorEngine.compute()
                │
                ▼
        IndicatorSnapshot
        (VWAP, SMA20/50/200, RSI, MACD, ATR, RelVol,
         candle structure, S/R levels, breakout/breakdown,
         spread, bid-ask imbalance)
                │
                ▼
        ScoringEngine.score()
                │
                ▼
        ScoreResult
        (long_score 0–100, short_score 0–100,
         confidence_label, top reasons)
                │
                ├──► GuidanceEngine.compute_guidance()
                │         └──► GuidanceResult
                │              (entry, stop, T1, T2, R/R, exit warning)
                │
                └──► AlertEngine.check_alerts()
                          └──► List[Alert]
                               (LONG_SETUP, SHORT_SETUP, EXIT, NO_TRADE, REVERSAL_RISK)
                                    │
                                    └──► PaperTradeLogger.log_signal()
                                              └──► paper_trades_YYYYMMDD.csv
```

---

## Module Reference

### `intraday/market_data_provider.py`

**Abstract interface** (`MarketDataProvider`) with three required methods:

```python
class MarketDataProvider(ABC):
    def get_latest_quote(self, symbol: str) -> QuoteData: ...
    def get_intraday_candles(self, symbol, bar_size, duration, use_rth) -> DataFrame: ...
    def get_daily_candles(self, symbol, duration) -> DataFrame: ...
    def get_order_book(self, symbol) -> Optional[OrderBookData]: ...   # optional
    def is_connected(self) -> bool: ...
```

**Concrete implementation** (`IBKRMarketDataProvider`):
- Uses the shared `IBKRConnector` singleton (already connected on app startup).
- `get_latest_quote()` — reads from the persistent `reqMktData` subscription; extracts `bidSize`/`askSize` directly from the `ib_insync` Ticker object.
- `get_order_book()` — calls `reqMktDepth` asynchronously on the ib_insync background event loop; returns `None` gracefully if the subscription is unavailable.
- `get_intraday_candles()` / `get_daily_candles()` — delegate to `IBKRConnector.fetch_historical_data()`.

**Data models** (all typed dataclasses):

| Model | Key fields |
|---|---|
| `QuoteData` | `last`, `bid`, `ask`, `bid_size`, `ask_size`, `spread`, `spread_pct`, `bid_ask_imbalance` |
| `OrderBookData` | `bids: List[OrderBookLevel]`, `asks: List[OrderBookLevel]`, `book_imbalance` |
| `OrderBookLevel` | `price`, `size`, `side`, `position` |

---

### `intraday/indicator_engine.py`

Single entry point: `IndicatorEngine.compute(intraday_df, quote, daily_df) → IndicatorSnapshot`

All results are returned in the typed `IndicatorSnapshot` dataclass — the scoring layer never touches raw DataFrames.

| Indicator | Method | Notes |
|---|---|---|
| VWAP | `_compute_vwap()` | Daily reset — only today's bars used |
| SMA20, SMA50 | `_compute_smas()` | Computed on 1-min closes |
| SMA200 | `_compute_smas()` | Falls back to daily bars if < 200 intraday bars available |
| SMA50 slope | `_compute_sma50_slope()` | Linear regression over configurable N bars |
| RSI(14) | `_compute_rsi()` | Standard Wilder smoothing |
| MACD(12,26,9) | `_compute_macd()` | Stores line, signal, histogram, and previous histogram |
| ATR(14) | `_compute_atr()` | True Range max of three ranges |
| Relative Volume | `_compute_rel_volume()` | Current bar vol ÷ 20-bar rolling average |
| Candle structure | `_compute_candle_structure()` | Body %, upper wick %, lower wick % |
| S/R levels | `_compute_sr_levels()` | Pivot high/low scan → zone clustering |
| Breakout/breakdown | `_compute_breakout()` | Price must exceed S/R by `breakout_confirm_pct` |

**Support & Resistance algorithm:**
1. Scan the last `sr_lookback` (default 100) bars for pivot highs and lows (local max/min over ±N bars).
2. Cluster pivots within `sr_zone_pct` (default 0.3%) of each other into a single zone.
3. Zones touched by `sr_min_touches` (default 2) or more bars are recorded as levels.
4. A breakout is confirmed when price exceeds the nearest resistance by `breakout_confirm_pct` (default 0.2%).

---

### `intraday/scoring_engine.py`

Loads weights from `config/intraday_scoring.yaml`. Evaluates ~20 directional factors. Each factor that fires adds its weight to the **long pool** or **short pool**.

```
long_score  = 100 × (fired long weights)  / (all long weights)
short_score = 100 × (fired short weights) / (all short weights)
```

**Confidence label assignment:**

| Condition | Label |
|---|---|
| `\|long − short\| < no_trade_gap (15)` | No Trade |
| dominant side score ≥ 68 | Strong Long / Strong Short |
| dominant side score ≥ 50 | Weak Long / Weak Short |
| dominant side score < 50 | No Trade |

**Output** (`ScoreResult`):

```python
@dataclass
class ScoreResult:
    long_score: float          # 0–100
    short_score: float         # 0–100
    confidence_label: str      # "Strong Long" | "Weak Long" | "No Trade" | ...
    dominant_side: str         # "long" | "short" | "none"
    long_reasons: List[str]    # fired bullish factor descriptions
    short_reasons: List[str]   # fired bearish factor descriptions
    rsi_overbought: bool
    rsi_oversold: bool
```

---

### `intraday/guidance_engine.py`

`GuidanceEngine.compute_guidance(score, snap, prev_score) → GuidanceResult`

**Entry price:** Current market price (educational — assumes market-order fill).

**Stop loss:** Chooses the *tighter* of:
- ATR-based stop: `entry ± ATR × stop_atr_mult` (default 1.0×)
- S/R-based stop: just below nearest support (long) or just above nearest resistance (short)

**Targets:**
- T1 = `entry ± ATR × target1_atr_mult` (default 1.5×)
- T2 = `entry ± ATR × target2_atr_mult` (default 2.5×)
- If no ATR available, falls back to `risk × 1.5` and `risk × 2.5`

**Exit warning** is raised when any of the following are true:
- Dominant score dropped ≥ 15 points vs. previous cycle
- RSI overbought on a long setup
- RSI oversold on a short setup
- Confidence label is only "Weak"
- `|long − short|` gap narrowed below 20

---

### `intraday/alert_engine.py`

`AlertEngine.check_alerts(score, snap, prev_score) → List[Alert]`

| Alert type | Trigger |
|---|---|
| `LONG_SETUP` | `long_score ≥ 65` and dominant side is long |
| `SHORT_SETUP` | `short_score ≥ 65` and dominant side is short |
| `EXIT_LONG` | Previous dominant was long; long score dropped ≥ 15 pts |
| `EXIT_SHORT` | Previous dominant was short; short score dropped ≥ 15 pts |
| `NO_TRADE` | Confidence label is "No Trade" |
| `REVERSAL_RISK` | Opposing score jumped ≥ 20 pts in one cycle |

All thresholds are configurable via `config/intraday_scoring.yaml → alerts`.

---

### `intraday/paper_trade_logger.py`

Writes to `logs/paper_trades/paper_trades_YYYYMMDD.csv`.

**CSV schema:**

| Column | Description |
|---|---|
| `timestamp` | UTC ISO-8601 |
| `ticker` | Symbol |
| `mode` | `live` or `backtest` |
| `long_score` | 0–100 |
| `short_score` | 0–100 |
| `confidence_label` | Confidence label string |
| `signal_type` | Alert type or label |
| `suggested_entry` | Price at signal |
| `stop_loss` | Computed stop |
| `target1` | First target |
| `target2` | Second target |
| `reward_risk` | (T1 − entry) / (entry − stop) |
| `atr` | ATR(14) at time of signal |
| `rsi14` | RSI at time of signal |
| `vwap` | VWAP at time of signal |
| `rel_volume` | Relative volume ratio |
| `outcome_pnl` | PnL per share (backtest only) |
| `outcome_note` | `target1`, `target2`, `stop`, or `eod` |

---

### `intraday/backtest_mode.py`

`IntradayBacktester.run(symbol, target_date, duration) → BacktestResult`

**Replay logic (bar-by-bar):**

1. Fetch 1-min historical bars from IBKR for the target date.
2. For each bar (after `warmup_bars` warm-up period):
   - Build a synthetic `QuoteData` from OHLCV (no live bid/ask in backtest).
   - Compute `IndicatorSnapshot`, `ScoreResult`, `GuidanceResult`.
   - If no open trade and dominant score ≥ `min_signal_score`: open a simulated trade.
   - If trade is open: check if the bar's high/low hit stop, T1, or T2.
3. Close any trade still open at end-of-day at the last bar's close.
4. Aggregate win rate, avg winner/loser, gross PnL per share.

**BacktestResult fields:**

```python
total_signals:  int      # bars scored
total_trades:   int      # trades opened
winning_trades: int
losing_trades:  int
win_rate:       float    # 0.0–1.0
gross_pnl:      float    # cumulative PnL per share
avg_winner:     float
avg_loser:      float
trades:         List[BacktestTrade]
```

---

## Scoring Logic

### Factor weights (from `config/intraday_scoring.yaml`)

#### Bullish factors

| Factor | Weight | Description |
|---|---|---|
| `price_above_vwap` | 2.0 | Price above intraday VWAP |
| `sma20_above_sma50` | 1.5 | SMA20 > SMA50 (short-term uptrend) |
| `sma50_slope_positive` | 1.2 | SMA50 slope rising over last 5 bars |
| `price_above_sma20` | 1.0 | Price above SMA20 |
| `price_above_sma50` | 0.8 | Price above SMA50 |
| `rsi_bullish_zone` | 1.5 | RSI 55–72 (healthy momentum) |
| `macd_bullish` | 1.5 | MACD line > Signal line |
| `macd_hist_rising` | 0.8 | MACD histogram increasing |
| `volume_spike` | 1.5 | Relative volume > 1.5× |
| `volume_extreme` | 2.0 | Relative volume > 2.5× |
| `bid_ask_bullish` | 1.8 | Bid size > Ask size |
| `tight_spread` | 0.4 | Spread < 5 bps |
| `breakout_above_resistance` | 2.5 | Price broke above resistance |
| `bullish_candle` | 0.7 | Close ≥ Open |
| `large_lower_wick_long` | 0.6 | Lower wick ≥ 50% of candle range |

#### Bearish factors (symmetric)

| Factor | Weight | Description |
|---|---|---|
| `price_below_vwap` | 2.0 | Price below intraday VWAP |
| `sma20_below_sma50` | 1.5 | SMA20 < SMA50 |
| `sma50_slope_negative` | 1.2 | SMA50 slope falling |
| `price_below_sma20` | 1.0 | Price below SMA20 |
| `price_below_sma50` | 0.8 | Price below SMA50 |
| `rsi_bearish_zone` | 1.5 | RSI 28–45 |
| `macd_bearish` | 1.5 | MACD line < Signal line |
| `macd_hist_falling` | 0.8 | MACD histogram decreasing |
| `bid_ask_bearish` | 1.8 | Ask size > Bid size |
| `breakdown_below_support` | 2.5 | Price broke below support |
| `bearish_candle` | 0.7 | Close < Open |
| `large_upper_wick_short` | 0.6 | Upper wick ≥ 50% of candle range |

### Normalisation formula

$$\text{score}_{\text{long}} = 100 \times \frac{\displaystyle\sum_{\text{fired}} w_{\text{long}}}{\displaystyle\sum_{\text{all}} w_{\text{long}}}$$

$$\text{score}_{\text{short}} = 100 \times \frac{\displaystyle\sum_{\text{fired}} w_{\text{short}}}{\displaystyle\sum_{\text{all}} w_{\text{short}}}$$

If $|\text{long} - \text{short}| < 15$ → **No Trade**.

---

## Indicator Reference

| Indicator | Parameters | Purpose in scoring |
|---|---|---|
| VWAP | Intraday daily reset | Primary trend reference; price above/below = +/-2.0 pts |
| SMA20 | 20 bars of 1-min closes | Short-term trend alignment |
| SMA50 | 50 bars | Medium-term trend; slope also evaluated |
| SMA200 | 200 bars (fallback: daily) | Long-term context |
| RSI | 14-period, Wilder smoothing | Momentum quality: 55–72 = bullish zone, 28–45 = bearish zone |
| MACD | 12 EMA − 26 EMA; 9-period signal | Crossover direction + histogram momentum |
| ATR | 14-period True Range average | Stop/target sizing |
| Relative Volume | Current bar ÷ 20-bar avg | Volume confirmation; > 1.5× = spike, > 2.5× = institutional |
| Bid-Ask Imbalance | bid_size ÷ (bid_size + ask_size) | Order-flow proxy; > 60% bids = buying pressure |
| Spread | ask − bid (absolute + %) | Market quality indicator |
| S/R levels | Pivot-based, last 100 bars | Key price levels for stop placement and breakout detection |

---

## Configuration

All scoring parameters live in `config/intraday_scoring.yaml`.

### Key threshold fields

```yaml
thresholds:
  no_trade_gap: 15          # min score gap to avoid "No Trade" label
  strong_threshold: 68      # score ≥ 68 → Strong Long/Short
  weak_threshold: 50        # score ≥ 50 → Weak Long/Short
  vol_spike_ratio: 1.5      # relative volume threshold for spike
  vol_extreme_ratio: 2.5    # relative volume threshold for extreme
  rsi_bull_low: 55          # RSI bullish zone lower bound
  rsi_bull_high: 72         # RSI bullish zone upper bound
  rsi_bear_low: 28          # RSI bearish zone lower bound
  rsi_bear_high: 45         # RSI bearish zone upper bound
  rsi_overbought: 75        # RSI overbought warning
  rsi_oversold: 25          # RSI oversold warning
  imbalance_threshold: 0.60 # bid fraction to count as "bullish imbalance"
  breakout_confirm_pct: 0.002  # must exceed S/R by 0.2% to confirm
  stop_atr_mult: 1.0        # ATR × this = stop distance
  target1_atr_mult: 1.5     # ATR × this = T1 distance
  target2_atr_mult: 2.5     # ATR × this = T2 distance
```

**To change a weight without touching code**, edit the `scoring:` section:

```yaml
scoring:
  breakout_above_resistance:
    weight: 3.0        # increase breakout importance
    side: long
    description: "Price broke above identified resistance level"
```

Hot-reload at runtime:

```python
scoring_engine.reload_config()
```

### Refresh interval

```yaml
refresh:
  poll_interval_secs: 5      # Streamlit auto-rerun interval
  candle_bar_size: "1 min"
  intraday_duration: "1 D"
  daily_duration: "250 D"
```

---

## Streamlit UI

### Page: ⚡ Intraday Guidance

**Accessible from the sidebar** under `⚡ Intraday Guidance`.

#### Layout

```
┌─────────────────────────────────────────────────────────────┐
│  [Ticker input]    [IBKR status]    [🔄 Refresh Now]        │
├──────────────────────┬──────────────────────────────────────┤
│  Score Panel         │  1-min Candlestick Chart             │
│  • Long %  Short %   │  • VWAP (orange dotted)              │
│  • Progress bars     │  • SMA20 (blue), SMA50 (purple)      │
│  • Confidence label  │  • SMA200 (brown dashed)             │
│  • Live quote strip  │  • S/R lines (green/red)             │
│    (bid/ask/sizes)   │  • Long ▲ / Short ▼ signal markers   │
│  • Bid-ask imbalance │                                      │
│  • Top reasons       │  Volume sub-panel (coloured bars)    │
├──────────────────────┴──────────────────────────────────────┤
│  Guidance Panel                                              │
│  Direction | Entry | Stop | T1 | T2 | R/R ratio             │
│  Entry basis description | Stop basis description            │
│  Exit warning (if present)                                   │
├─────────────────────────────────────────────────────────────┤
│  Indicator Metrics (8 columns)                               │
│  RSI | MACD | MACD Signal | ATR | VWAP | Rel Vol            │
│  Spread % | Bid/Ask Imbalance                                │
│  SMA20 | SMA50 | SMA200 | Breakout | Breakdown | Nearest S/R│
├─────────────────────────────────────────────────────────────┤
│  Alert Feed (rolling, newest first)                          │
├─────────────────────────────────────────────────────────────┤
│  Tabs: [📋 Paper-Trade Log]  [🧪 Backtest Simulation]       │
└─────────────────────────────────────────────────────────────┘
```

#### Auto-refresh

The page calls `st.rerun()` every 5 seconds to poll fresh data. Click **🔄 Refresh Now** for an immediate update.

#### Demo mode

When IBKR is not connected, the page renders a synthetic demo chart with randomly generated price data so the UI layout is still visible.

---

## Backtesting & Paper-Trade Log

### Running a backtest from the UI

1. Navigate to **⚡ Intraday Guidance**.
2. Click the **🧪 Backtest Simulation** tab.
3. Enter the ticker, select a historical date, and set the minimum signal score threshold.
4. Click **▶️ Run Backtest**.

Results show:
- Total signals evaluated
- Win rate, gross PnL per share, avg winner, avg loser
- Trade-by-trade table with entry, stop, targets, exit price, exit reason, PnL

All backtest events are also written to the daily paper-trade CSV log in `logs/paper_trades/`.

### Paper-trade log location

```
logs/paper_trades/paper_trades_20260311.csv
```

Filter by ticker in the **📋 Paper-Trade Log** tab within the UI.

---

## Running Locally

### Prerequisites

- IBKR Gateway or TWS running and API connections enabled
- Paper trading port: `4002` | Live port: `4001` / `7496`
- Python virtual environment with all dependencies installed

### Start

```bash
cd unified-trading-system
source venv/bin/activate
streamlit run streamlit_app.py
```

Navigate to **⚡ Intraday Guidance** in the sidebar. Enter a ticker and the system will auto-connect using the shared IBKR singleton.

### Environment variables (`.env`)

```ini
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # 4002=paper, 4001=live
IBKR_CLIENT_ID=1
TRADING_MODE=paper
```

---

## Limitations & Improvement Roadmap

### Current limitations

| Area | Limitation | Impact |
|---|---|---|
| **Level-2 data** | `reqMktDepth` requires an IBKR L2 market-data subscription. Without it, `get_order_book()` returns `None` gracefully and the scoring falls back to L1 bid/ask sizes only. | Bid-ask imbalance is less granular without full order book depth. |
| **Tape / time-and-sales** | True tape aggression (trades hitting bid vs. ask) is approximated via bid-size dominance. Requires `reqTickByTickData` (not yet wired). | Cannot distinguish passive fills from aggressive trades. |
| **SMA200 warm-up** | SMA200 on 1-min bars requires 200 bars (~3.3 hours). Falls back to daily bars automatically, but intraday SMA200 is not available at market open. | SMA200 may reflect prior-day levels early in the session. |
| **Backtest slippage** | Simulated fills assume the exact signal price with no slippage or commission. | Backtest PnL will be optimistic vs. live execution. |
| **Single ticker** | Each page session tracks one ticker at a time. | Cannot monitor a basket simultaneously from one view. |
| **No options flow** | Dark pool / unusual options activity is not considered. | Misses a significant institutional signal source. |
| **Fixed bar size** | Scoring uses 1-min bars exclusively. | Cannot adapt to slower or faster intraday setups (e.g., 5-min swing). |

### Improvement roadmap

#### Priority 1 — Data depth

1. **Wire `reqTickByTickData`** for true tape prints.  
   Add `tape_aggression_ratio` (buy prints ÷ total prints) as a scored factor.  
   Suggested weight: `2.0` for long, `2.0` for short.

2. **Wire `reqMktDepth`** and persist the subscription (currently it opens/closes on each call).  
   Cache the full order-book quote and expose:
   - `book_imbalance` (total bid depth vs. ask depth across all levels)
   - `depth_steepness` (volume concentration near best bid/ask)

3. **Pre-market data**: extend `use_rth=False` option to capture pre-market context (gap analysis, volume, price action).

#### Priority 2 — Scoring model

4. **Multi-timeframe confirmation**: add a 5-min bar window. Require that both 1-min and 5-min scores agree before issuing a "Strong" signal. Reduces false positives significantly.

5. **Adaptive weights**: record which factors fire at each signal and compare against actual outcome (from backtest). Implement a simple regression to recalibrate weights periodically.

6. **Options flow factor**: integrate unusual options activity (OI, vol/OI ratio) from a secondary provider (e.g., Market Chameleon API) as an optional factor.

7. **Gap-fill scoring**: detect opening gaps and add a "gap-fill probability" factor — strong mean-reversion edge early in session.

#### Priority 3 — Risk & execution quality

8. **Slippage model**: add `slippage_pct` and `commission_per_share` parameters to the backtester for more realistic net PnL.

9. **Intraday max-loss stop**: add a per-ticker `max_intraday_loss` circuit breaker that suppresses signals after a configurable drawdown.

10. **ATR-scaled position sizing**: display a suggested share-count alongside guidance based on account equity and `max_risk_pct` from `trading_config.yaml`.

#### Priority 4 — Multi-ticker & performance

11. **Multi-ticker mode**: store per-ticker session state under `igt_{ticker}_*` keys. Render a compact summary table for a watchlist. Already architecturally ready — each ticker just needs its own engine instances.

12. **WebSocket-based push**: replace the 5-second polling loop with an `ib_insync` event callback (`ticker.updateEvent`) that triggers `st.session_state` updates via a queue — latency drops from ~5 s to < 100 ms.

13. **Persistent indicator cache**: store rolling `IndicatorSnapshot` history in a lightweight SQLite database (one table per ticker). Enables lookback comparisons and longer alert history across sessions.

#### Priority 5 — UI enhancements

14. **RSI sub-panel**: add a third Plotly row showing the RSI line with overbought/oversold bands.

15. **MACD sub-panel**: add a fourth Plotly row with MACD line, signal, and histogram.

16. **Score history sparkline**: show a 30-bar mini-chart of how `long_score` and `short_score` evolved over the session.

17. **Audio alerts**: use a browser notification or a simple `st.audio()` tone when a Strong Long/Short setup fires.

---

## Example Scoring Walkthrough

> Ticker: **AAPL** | Time: 10:15 ET | Price: $220.50

| Factor | Fired? | Weight | Side |
|---|---|---|---|
| price_above_vwap (VWAP = $219.80) | ✅ | 2.0 | long |
| sma20_above_sma50 (SMA20=$220.10, SMA50=$219.70) | ✅ | 1.5 | long |
| sma50_slope_positive | ✅ | 1.2 | long |
| price_above_sma20 | ✅ | 1.0 | long |
| price_above_sma50 | ✅ | 0.8 | long |
| rsi_bullish_zone (RSI=61.3) | ✅ | 1.5 | long |
| macd_bullish | ✅ | 1.5 | long |
| macd_hist_rising | ✅ | 0.8 | long |
| volume_spike (rel_vol=1.8×) | ✅ | 1.5 | both |
| bid_ask_bullish (imbalance=65%) | ✅ | 1.8 | long |
| breakout_above_resistance ($220.20) | ✅ | 2.5 | long |
| bullish_candle | ✅ | 0.7 | long |

**Long pool fired:** 2.0 + 1.5 + 1.2 + 1.0 + 0.8 + 1.5 + 1.5 + 0.8 + 1.5 + 1.8 + 2.5 + 0.7 = **16.8**  
**Long pool max:** ~21.4  
**Long score:** `100 × 16.8 / 21.4 ≈ 79` → **Strong Long 🟢🟢**

**Guidance output:**
- Entry: ~$220.50  
- Stop: $219.80 (below VWAP / ATR stop, whichever is closer)  
- T1: $221.15 (ATR × 1.5 = $0.65 above entry)  
- T2: $221.58 (ATR × 2.5)  
- Est. R/R: ~1.5×

---

*Last updated: March 2026 | Unified Trading System — Phase 1*
