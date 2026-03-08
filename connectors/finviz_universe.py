"""
Finviz Elite – Dynamic Universe Loader & Scalping Scorer
=========================================================
Downloads the **complete** Finviz Elite universe (no server-side filters)
every 5 minutes, scores every security for intraday/scalping suitability
using a capital-preservative framework, and returns the top-N candidates.

Capital-preservative scalping philosophy
-----------------------------------------
We want tickers that:
  1. Are liquid enough to enter/exit instantly with tight spreads.
  2. Move enough intraday to be worth trading (ATR sweet-spot).
  3. Are stable enough that a bad trade can be recovered in 1-2 sessions.
  4. Are NOT gap-ridden news-driven bombs that can spike 20%+ against you.

Usage
-----
    from connectors.finviz_universe import FinvizEliteUniverse
    loader = FinvizEliteUniverse()
    top10  = loader.get_top_scalping_picks(n=10)   # list[str]
    detail = loader.get_scored_universe()           # pd.DataFrame with scores
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from io import StringIO
from typing import List, Optional, Tuple

import pandas as pd
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Configuration defaults (override via constructor)
# ─────────────────────────────────────────────────────────────────────────────
FINVIZ_ELITE_AUTH   = "b1c2678d-5a53-4608-8be5-072dad4a4264"
FINVIZ_EXPORT_URL   = "https://elite.finviz.com/export.ashx"
CACHE_TTL_SECONDS   = 300          # 5 minutes
REQUEST_TIMEOUT     = 120          # seconds
TOP_N_DEFAULT       = 10

# ── Scoring weights (must sum to 1.0) ───────────────────────────────────────
W_LIQUIDITY         = 0.45   # Rel-Volume + Avg-Volume
W_ATR_SWEET_SPOT    = 0.25   # ATR% ideally 1.5–4.5 %
W_STABILITY         = 0.20   # Market-cap size + bounded day-change
W_PRICE_SWEET_SPOT  = 0.10   # $10–$300 ideal for retail scalping


class FinvizEliteUniverse:
    """
    Downloads Finviz Elite complete universe and scores for scalping.

    The download uses the auth-token API (no login cookies needed).
    Results are cached for `cache_ttl` seconds to avoid hammering the API.
    """

    def __init__(
        self,
        auth_token: str = FINVIZ_ELITE_AUTH,
        cache_ttl: int  = CACHE_TTL_SECONDS,
    ):
        self.auth_token  = auth_token
        self.cache_ttl   = cache_ttl

        # Internal cache
        self._raw_df:      Optional[pd.DataFrame] = None
        self._scored_df:   Optional[pd.DataFrame] = None
        self._last_fetch:  Optional[float]        = None   # unix timestamp

        self._session = self._build_session()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_top_scalping_picks(self, n: int = TOP_N_DEFAULT, force_refresh: bool = False) -> List[str]:
        """
        Return the top-N ticker symbols ranked for capital-preservative scalping.

        Parameters
        ----------
        n : int
            Number of tickers to return (default 10).
        force_refresh : bool
            Bypass the cache and re-download even if TTL has not expired.

        Returns
        -------
        list[str]  – ticker symbols, best first.
        """
        df = self.get_scored_universe(force_refresh=force_refresh)
        if df is None or df.empty:
            return []
        return df["Ticker"].head(n).tolist()

    def get_scored_universe(self, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        """
        Return the full scored + ranked universe DataFrame.

        Columns include all Finviz fields plus:
          Scalping_Score, Liquidity_Score, ATR_Score, Stability_Score,
          Price_Score, ATR_Pct, Rank

        Returns None on a download failure (stale cache is returned if available).
        """
        if not force_refresh and self._cache_valid():
            return self._scored_df

        raw = self._download_universe()
        if raw is None or raw.empty:
            # Return stale cache rather than nothing
            return self._scored_df

        self._raw_df    = raw
        self._scored_df = self._score_and_rank(raw)
        self._last_fetch = time.monotonic()
        return self._scored_df

    @property
    def last_refresh_time(self) -> Optional[datetime]:
        """Wall-clock time of the last successful download (UTC), or None."""
        if self._last_fetch is None:
            return None
        elapsed = time.monotonic() - self._last_fetch
        return datetime.fromtimestamp(time.time() - elapsed, tz=timezone.utc)

    @property
    def seconds_until_next_refresh(self) -> int:
        """Seconds remaining before the cache expires (0 if already stale)."""
        if self._last_fetch is None:
            return 0
        remaining = self.cache_ttl - (time.monotonic() - self._last_fetch)
        return max(0, int(remaining))

    @property
    def universe_size(self) -> int:
        """Number of securities in the raw downloaded universe."""
        return len(self._raw_df) if self._raw_df is not None else 0

    # ─────────────────────────────────────────────────────────────────────────
    # Download
    # ─────────────────────────────────────────────────────────────────────────

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/csv,application/octet-stream,*/*",
        })
        return s

    def _cache_valid(self) -> bool:
        if self._scored_df is None or self._last_fetch is None:
            return False
        return (time.monotonic() - self._last_fetch) < self.cache_ttl

    def _download_universe(self) -> Optional[pd.DataFrame]:
        """
        Pull the complete Finviz Elite export CSV with NO server-side filters.
        The auth token is appended as a query parameter (Elite API method).
        """
        # No filter params → Finviz returns ALL securities
        url = f"{FINVIZ_EXPORT_URL}?auth={self.auth_token}"

        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            text = resp.text

            # Basic sanity check – must look like CSV
            if "text/html" in content_type.lower() or "<html" in text[:200].lower():
                raise ValueError("Received HTML – auth token may be invalid or expired")

            df = pd.read_csv(StringIO(text), on_bad_lines="skip")
            if df.empty:
                raise ValueError("Finviz returned an empty CSV")

            df = self._normalize(df)
            return df

        except Exception as exc:
            print(f"[FinvizEliteUniverse] Download error: {exc}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Normalisation
    # ─────────────────────────────────────────────────────────────────────────

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """Standardise column names and parse numeric fields."""

        # ── Column aliases ────────────────────────────────────────────────────
        rename = {}
        for col in df.columns:
            lower = col.strip().lower()
            if lower in ("ticker", "symbol"):
                rename[col] = "Ticker"
            elif lower == "price":
                rename[col] = "Price"
            elif lower in ("change", "chg", "change %"):
                rename[col] = "Change_Pct"
            elif lower in ("volume",):
                rename[col] = "Volume"
            elif lower in ("avg volume", "average volume", "avgvol"):
                rename[col] = "Avg_Volume"
            elif lower in ("rel volume", "relative volume", "relvol"):
                rename[col] = "Rel_Volume"
            elif lower in ("atr", "atr (14)"):
                rename[col] = "ATR"
            elif lower in ("market cap", "mkt cap", "marketcap"):
                rename[col] = "Market_Cap"
            elif lower in ("beta",):
                rename[col] = "Beta"
            elif lower in ("float", "shares float"):
                rename[col] = "Float"
            elif lower in ("rsi", "rsi (14)"):
                rename[col] = "RSI"
        if rename:
            df = df.rename(columns=rename)

        # ── Numeric parsing ───────────────────────────────────────────────────
        for col in ["Price", "Change_Pct", "ATR", "Rel_Volume", "Beta", "RSI"]:
            if col in df.columns:
                df[col] = df[col].apply(_parse_numeric)

        for col in ["Volume", "Avg_Volume", "Market_Cap", "Float"]:
            if col in df.columns:
                df[col] = df[col].apply(_parse_suffix)

        # ── Drop rows with no ticker or price ─────────────────────────────────
        if "Ticker" in df.columns:
            df = df[df["Ticker"].notna() & (df["Ticker"].str.strip() != "")]
        if "Price" in df.columns:
            df = df[df["Price"].notna() & (df["Price"] > 0)]

        df = df.reset_index(drop=True)
        return df

    # ─────────────────────────────────────────────────────────────────────────
    # Scoring
    # ─────────────────────────────────────────────────────────────────────────

    def _score_and_rank(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply capital-preservative scalping scoring framework, filter obviously
        unsuitable securities, sort descending by score.
        """
        df = df.copy()

        # ── Hard filters (absolute minimums for scalping) ─────────────────────
        if "Price" in df.columns:
            df = df[df["Price"] >= 5.0]          # No penny stocks
        if "Avg_Volume" in df.columns:
            df = df[df["Avg_Volume"] >= 300_000]  # Minimum avg daily volume
        if "Volume" in df.columns:
            df = df[df["Volume"] >= 50_000]       # Must have traded today
        # Exclude change% extremes that signal news-driven gaps (blow-up risk)
        if "Change_Pct" in df.columns:
            df = df[df["Change_Pct"].abs() <= 15.0]

        df = df.reset_index(drop=True)

        # ── Compute ATR% ──────────────────────────────────────────────────────
        if "ATR" in df.columns and "Price" in df.columns:
            df["ATR_Pct"] = (df["ATR"] / df["Price"] * 100).round(2)
        elif "ATR" in df.columns:
            df["ATR_Pct"] = None
        else:
            df["ATR_Pct"] = None

        # ── Component scores (0–100 each) ─────────────────────────────────────
        df["Liquidity_Score"]    = df.apply(_liquidity_score,    axis=1)
        df["ATR_Score"]          = df.apply(_atr_score,          axis=1)
        df["Stability_Score"]    = df.apply(_stability_score,    axis=1)
        df["Price_Score"]        = df.apply(_price_score,        axis=1)

        # ── Composite ─────────────────────────────────────────────────────────
        df["Scalping_Score"] = (
            df["Liquidity_Score"]  * W_LIQUIDITY       +
            df["ATR_Score"]        * W_ATR_SWEET_SPOT  +
            df["Stability_Score"]  * W_STABILITY       +
            df["Price_Score"]      * W_PRICE_SWEET_SPOT
        ).round(2)

        df = df.sort_values("Scalping_Score", ascending=False).reset_index(drop=True)
        df.index = df.index + 1
        df.index.name = "Rank"
        df = df.reset_index()

        return df


# ─────────────────────────────────────────────────────────────────────────────
# Scoring sub-functions (pure, row-level)
# ─────────────────────────────────────────────────────────────────────────────

def _liquidity_score(row) -> float:
    """
    45% of total score.
    Rewards very high relative volume (people are actively trading this today)
    and large average daily volume (tight spread, easy exit).
    """
    score = 0.0

    # Relative Volume (0–55 points within this component)
    rel = row.get("Rel_Volume") if isinstance(row, dict) else getattr(row, "Rel_Volume", None)
    if rel is not None and not _isnan(rel):
        rel = float(rel)
        if rel >= 5.0:
            rv_score = 100
        elif rel >= 3.0:
            rv_score = 85 + (rel - 3.0) / 2.0 * 15
        elif rel >= 2.0:
            rv_score = 70 + (rel - 2.0) * 15
        elif rel >= 1.5:
            rv_score = 55 + (rel - 1.5) / 0.5 * 15
        elif rel >= 1.0:
            rv_score = 40 + (rel - 1.0) / 0.5 * 15
        elif rel >= 0.5:
            rv_score = 15 + (rel - 0.5) / 0.5 * 25
        else:
            rv_score = max(0, rel / 0.5 * 15)
        score += rv_score * 0.55

    # Average Volume (0–45 points within component)
    avg_vol = row.get("Avg_Volume") if isinstance(row, dict) else getattr(row, "Avg_Volume", None)
    if avg_vol is not None and not _isnan(avg_vol):
        avg_vol = float(avg_vol)
        if avg_vol >= 10_000_000:
            av_score = 100
        elif avg_vol >= 5_000_000:
            av_score = 85 + (avg_vol - 5e6) / 5e6 * 15
        elif avg_vol >= 2_000_000:
            av_score = 70 + (avg_vol - 2e6) / 3e6 * 15
        elif avg_vol >= 1_000_000:
            av_score = 55 + (avg_vol - 1e6) / 1e6 * 15
        elif avg_vol >= 500_000:
            av_score = 35 + (avg_vol - 500_000) / 500_000 * 20
        elif avg_vol >= 300_000:
            av_score = 15 + (avg_vol - 300_000) / 200_000 * 20
        else:
            av_score = 0
        score += av_score * 0.45

    return round(score, 2)


def _atr_score(row) -> float:
    """
    25% of total score.
    Sweet spot: ATR% 1.5%–4.5%
      - Enough daily range to scalp profitably
      - Not so violent that a single adverse move is unrecoverable
    """
    atr_pct = row.get("ATR_Pct") if isinstance(row, dict) else getattr(row, "ATR_Pct", None)
    if atr_pct is None or _isnan(atr_pct):
        return 40.0   # Neutral score if ATR unknown

    atr_pct = float(atr_pct)

    if 1.5 <= atr_pct <= 4.5:
        # Core sweet-spot: full score, peak in the middle
        peak = 3.0
        distance = abs(atr_pct - peak) / 1.5   # 0 at peak, 1 at edges
        return round(100 - distance * 20, 2)    # 80–100
    elif 0.8 <= atr_pct < 1.5:
        # Too flat – still tradeable but less opportunity
        return round(30 + (atr_pct - 0.8) / 0.7 * 50, 2)
    elif 4.5 < atr_pct <= 6.0:
        # Slightly elevated – acceptable with tighter position size
        return round(80 - (atr_pct - 4.5) / 1.5 * 40, 2)
    elif 6.0 < atr_pct <= 10.0:
        # High volatility – risky for capital preservation
        return round(40 - (atr_pct - 6.0) / 4.0 * 35, 2)
    elif atr_pct > 10.0:
        # Extreme – avoid
        return max(0, round(5 - (atr_pct - 10.0), 2))
    else:
        # < 0.8 % – too flat, hardly any intraday movement
        return round(max(0, atr_pct / 0.8 * 30), 2)


def _stability_score(row) -> float:
    """
    20% of total score.
    Two components:
    (a) Market cap – bigger companies recover faster, have institutional support.
    (b) Day change % penalty – extreme movers are news-driven and unpredictable.
    """
    score = 0.0

    # (a) Market cap (0–70 points in component)
    mkt_cap = row.get("Market_Cap") if isinstance(row, dict) else getattr(row, "Market_Cap", None)
    if mkt_cap is not None and not _isnan(mkt_cap):
        mkt_cap = float(mkt_cap)
        if mkt_cap >= 100e9:          # Mega cap
            mc_score = 100
        elif mkt_cap >= 10e9:         # Large cap
            mc_score = 85 + math.log10(mkt_cap / 10e9) / math.log10(10) * 15
        elif mkt_cap >= 2e9:          # Mid cap
            mc_score = 65 + (mkt_cap - 2e9) / 8e9 * 20
        elif mkt_cap >= 500e6:        # Small cap
            mc_score = 40 + (mkt_cap - 500e6) / 1.5e9 * 25
        elif mkt_cap >= 200e6:        # Micro cap boundary
            mc_score = 15 + (mkt_cap - 200e6) / 300e6 * 25
        else:
            mc_score = 0
        score += mc_score * 0.70

    # (b) Day change penalty (0–30 points in component)
    chg = row.get("Change_Pct") if isinstance(row, dict) else getattr(row, "Change_Pct", None)
    if chg is not None and not _isnan(chg):
        chg_abs = abs(float(chg))
        # Moderate movement is fine; big gaps are risky (already gapped 15% is filtered)
        if chg_abs <= 2.0:
            chg_score = 100
        elif chg_abs <= 5.0:
            chg_score = 100 - (chg_abs - 2.0) / 3.0 * 30
        elif chg_abs <= 10.0:
            chg_score = 70 - (chg_abs - 5.0) / 5.0 * 50
        else:
            chg_score = max(0, 20 - (chg_abs - 10.0) * 4)
        score += chg_score * 0.30
    else:
        score += 50 * 0.30   # neutral when unknown

    return round(score, 2)


def _price_score(row) -> float:
    """
    10% of total score.
    Rewards a price bracket where:
    - Position sizing is practical for retail accounts ($10–$300)
    - Not so cheap it signals a distressed company (<$5 already filtered)
    - Not so expensive that 1 share = huge slice of capital
    """
    price = row.get("Price") if isinstance(row, dict) else getattr(row, "Price", None)
    if price is None or _isnan(price):
        return 50.0

    price = float(price)

    if 20 <= price <= 200:
        return 100.0
    elif 10 <= price < 20:
        return round(70 + (price - 10) / 10 * 30, 2)
    elif 200 < price <= 350:
        return round(100 - (price - 200) / 150 * 30, 2)
    elif 5 <= price < 10:
        return round(40 + (price - 5) / 5 * 30, 2)
    elif 350 < price <= 600:
        return round(70 - (price - 350) / 250 * 40, 2)
    elif price > 600:
        return max(10, round(30 - (price - 600) / 100 * 5, 2))
    else:
        return 20.0


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return True


def _parse_numeric(value) -> Optional[float]:
    """Parse plain float / percentage strings."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("%", "").replace("$", "")
    if s in ("", "-", "N/A", "n/a", "na"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_suffix(value) -> Optional[float]:
    """Parse strings like '1.5B', '300M', '2K' into floats."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "").upper()
    if s in ("", "-", "N/A", "NA"):
        return None
    multipliers = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    mult = 1.0
    if s and s[-1] in multipliers:
        mult = multipliers[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None
