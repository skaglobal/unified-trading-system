"""
Market Data Provider Abstraction
=================================
Defines a clean adapter interface so the underlying data source (IBKR,
Alpaca, Polygon, …) can be swapped without touching the analysis layer.

Concrete implementation: ``IBKRMarketDataProvider``

DISCLAIMER: For educational decision support only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("intraday.market_data_provider")


# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------

@dataclass
class QuoteData:
    """Snapshot of the latest best-bid/offer and last-trade data."""

    symbol: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None

    high: Optional[float] = None
    low: Optional[float] = None
    open: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[int] = None

    # Derived convenience fields
    spread: Optional[float] = None
    spread_pct: Optional[float] = None
    change_pct: Optional[float] = None

    source: str = "unknown"

    @property
    def mid(self) -> Optional[float]:
        """Mid-point price."""
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def bid_ask_imbalance(self) -> Optional[float]:
        """Bid/(Bid+Ask) fraction (0.5 = balanced; >0.5 = more bids = buying pressure)."""
        if self.bid_size and self.ask_size and (self.bid_size + self.ask_size) > 0:
            return self.bid_size / (self.bid_size + self.ask_size)
        return None


@dataclass
class OrderBookLevel:
    """Single level in the order book."""
    price: float
    size: int
    side: str   # "bid" or "ask"
    position: int  # 0 = best


@dataclass
class OrderBookData:
    """Level-2 order book snapshot."""
    symbol: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    @property
    def total_bid_size(self) -> int:
        return sum(l.size for l in self.bids)

    @property
    def total_ask_size(self) -> int:
        return sum(l.size for l in self.asks)

    @property
    def book_imbalance(self) -> Optional[float]:
        total = self.total_bid_size + self.total_ask_size
        if total > 0:
            return self.total_bid_size / total
        return None


# ---------------------------------------------------------------------------
# Abstract provider interface
# ---------------------------------------------------------------------------

class MarketDataProvider(ABC):
    """Abstract market-data provider.

    Implement this class to plug in a different data source (Alpaca, Polygon,
    Tradier, …).  The analysis layer only depends on this interface.
    """

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> QuoteData:
        """Return the latest L1 quote for *symbol*."""

    @abstractmethod
    def get_intraday_candles(
        self,
        symbol: str,
        bar_size: str = "1 min",
        duration: str = "1 D",
        use_rth: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Return a DataFrame of intraday OHLCV bars.

        Columns: Open, High, Low, Close, Volume (datetime index).
        """

    @abstractmethod
    def get_daily_candles(
        self,
        symbol: str,
        duration: str = "250 D",
    ) -> Optional[pd.DataFrame]:
        """Return a DataFrame of daily OHLCV bars (for longer SMAs)."""

    def get_order_book(self, symbol: str) -> Optional[OrderBookData]:
        """Return Level-2 order book data (optional — providers may return None)."""
        return None

    def is_connected(self) -> bool:
        """Return True if the provider has an active data connection."""
        return True


# ---------------------------------------------------------------------------
# IBKR concrete implementation
# ---------------------------------------------------------------------------

class IBKRMarketDataProvider(MarketDataProvider):
    """Concrete market-data provider backed by Interactive Brokers via ib_insync.

    Args:
        ibkr_connector: A live ``IBKRConnector`` instance (shared singleton).
        depth_rows: Number of L2 rows to request per side (default 5).
    """

    def __init__(self, ibkr_connector: Any, depth_rows: int = 5) -> None:
        self._ibkr = ibkr_connector
        self._depth_rows = depth_rows
        # Per-symbol depth subscriptions: sym -> (ticker, event)
        self._depth_subs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # L1 Quote
    # ------------------------------------------------------------------

    def get_latest_quote(self, symbol: str) -> QuoteData:
        """Fetch live L1 quote for *symbol* via persistent IBKR subscription."""
        raw: Dict[str, Any] = {}
        try:
            raw_map = self._ibkr.get_live_quotes([symbol], wait_secs=1.5)
            raw = raw_map.get(symbol, {})
        except Exception as exc:
            logger.warning("get_latest_quote(%s) failed: %s", symbol, exc)

        def _v(key: str) -> Optional[float]:
            val = raw.get(key)
            try:
                f = float(val)
                return None if (math.isnan(f) or f == 0.0) else f
            except (TypeError, ValueError):
                return None

        def _i(key: str) -> Optional[int]:
            val = raw.get(key)
            try:
                f = float(val)
                return None if (math.isnan(f) or f <= 0) else int(f)
            except (TypeError, ValueError):
                return None

        last = _v("last")
        bid = _v("bid")
        ask = _v("ask")

        spread: Optional[float] = None
        spread_pct: Optional[float] = None
        if bid and ask and ask > bid:
            spread = round(ask - bid, 4)
            if last and last > 0:
                spread_pct = round(spread / last * 100, 3)

        prev_close = _v("close")
        change_pct: Optional[float] = None
        if last and prev_close and prev_close > 0:
            change_pct = round((last - prev_close) / prev_close * 100, 2)

        # Fetch bid/ask sizes via direct ticker scan from ib_insync object
        bid_size: Optional[int] = None
        ask_size: Optional[int] = None
        try:
            ticker = self._ibkr._live_subs.get(symbol)
            if ticker is not None:
                bs = getattr(ticker, "bidSize", None)
                as_ = getattr(ticker, "askSize", None)
                if bs is not None:
                    try:
                        bsf = float(bs)
                        bid_size = int(bsf) if not math.isnan(bsf) and bsf > 0 else None
                    except (TypeError, ValueError):
                        pass
                if as_ is not None:
                    try:
                        asf = float(as_)
                        ask_size = int(asf) if not math.isnan(asf) and asf > 0 else None
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        return QuoteData(
            symbol=symbol,
            timestamp=datetime.utcnow(),
            last=last,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            high=_v("high"),
            low=_v("low"),
            open=_v("open"),
            prev_close=prev_close,
            volume=_i("volume"),
            spread=spread,
            spread_pct=spread_pct,
            change_pct=change_pct,
            source="IBKR",
        )

    # ------------------------------------------------------------------
    # Historical bars
    # ------------------------------------------------------------------

    def get_intraday_candles(
        self,
        symbol: str,
        bar_size: str = "1 min",
        duration: str = "1 D",
        use_rth: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Fetch intraday OHLCV bars from IBKR historical data API.

        Returns a DataFrame with a DatetimeIndex (timezone-aware) and
        columns: Open, High, Low, Close, Volume.
        """
        try:
            df = self._ibkr.fetch_historical_data(
                symbol=symbol,
                duration=duration,
                bar_size=bar_size,
                what_to_show="TRADES",
                use_rth=use_rth,
            )
            if df is None or df.empty:
                return None
            df = self._normalise_ohlcv(df)
            return df
        except Exception as exc:
            logger.error("get_intraday_candles(%s) error: %s", symbol, exc)
            return None

    def get_daily_candles(
        self,
        symbol: str,
        duration: str = "250 D",
    ) -> Optional[pd.DataFrame]:
        """Fetch daily OHLCV bars (for SMA50/SMA200 calculation)."""
        try:
            df = self._ibkr.fetch_historical_data(
                symbol=symbol,
                duration=duration,
                bar_size="1 day",
                what_to_show="TRADES",
                use_rth=True,
            )
            if df is None or df.empty:
                return None
            df = self._normalise_ohlcv(df)
            return df
        except Exception as exc:
            logger.error("get_daily_candles(%s) error: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Level 2 / order book (optional)
    # ------------------------------------------------------------------

    def get_order_book(self, symbol: str) -> Optional[OrderBookData]:
        """Fetch Level-2 market depth from IBKR.

        Requires market-data subscriptions that include L2 data.
        Returns None gracefully if unavailable.
        """
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_get_order_book(symbol), self._ibkr._loop
            )
            return future.result(timeout=8)
        except Exception as exc:
            logger.debug("get_order_book(%s) unavailable: %s", symbol, exc)
            return None

    async def _async_get_order_book(self, symbol: str) -> Optional[OrderBookData]:
        """Async order-book fetch running on ib_insync's event loop."""
        ib = self._ibkr.ib
        if ib is None or not ib.isConnected():
            return None

        # Resolve qualified contract from the existing subscriptions cache
        contract = self._ibkr._live_contracts.get(symbol)
        if contract is None:
            try:
                raw = self._ibkr._create_contract(symbol)
                qualified = await ib.qualifyContractsAsync(raw)
                if not qualified:
                    return None
                contract = qualified[0]
            except Exception as exc:
                logger.debug("Qualify for L2 failed %s: %s", symbol, exc)
                return None

        try:
            ticker = ib.reqMktDepth(contract, numRows=self._depth_rows)
            await asyncio.sleep(1.5)

            bids: List[OrderBookLevel] = []
            asks: List[OrderBookLevel] = []

            dom = ticker.domBids if hasattr(ticker, "domBids") else []
            for i, level in enumerate(dom):
                try:
                    bids.append(OrderBookLevel(
                        price=float(level.price),
                        size=int(level.size),
                        side="bid",
                        position=i,
                    ))
                except Exception:
                    pass

            dom = ticker.domAsks if hasattr(ticker, "domAsks") else []
            for i, level in enumerate(dom):
                try:
                    asks.append(OrderBookLevel(
                        price=float(level.price),
                        size=int(level.size),
                        side="ask",
                        position=i,
                    ))
                except Exception:
                    pass

            ib.cancelMktDepth(contract)

            return OrderBookData(
                symbol=symbol,
                timestamp=datetime.utcnow(),
                bids=bids,
                asks=asks,
            )
        except Exception as exc:
            logger.debug("reqMktDepth(%s) error: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Connection check
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        return self._ibkr.is_connected()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure canonical column names and a DatetimeIndex."""
        col_map = {
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # Ensure datetime index
        if not isinstance(df.index, pd.DatetimeIndex):
            # ib_insync returns a 'date' column when the index is a date object
            if "date" in df.columns:
                df = df.set_index(pd.to_datetime(df["date"]))
            else:
                df.index = pd.to_datetime(df.index)

        # Drop rows with all-NaN OHLCV
        ohlcv_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df.dropna(subset=ohlcv_cols, how="all")

        return df


# ---------------------------------------------------------------------------
# Simple polling thread helper (optional — Streamlit uses st.rerun instead)
# ---------------------------------------------------------------------------

class PollingThread:
    """Background thread that calls *callback* every *interval_secs* seconds.

    Used for non-Streamlit environments (CLI, backtest replay, etc.).
    """

    def __init__(self, callback, interval_secs: float = 5.0) -> None:
        self._callback = callback
        self._interval = interval_secs
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._callback()
            except Exception as exc:
                logger.error("PollingThread callback error: %s", exc)
