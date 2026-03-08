"""
IBKR Connector - Interactive Brokers connection management using ib_insync.

Combines best practices from multiple implementations with Python 3.11+ compatibility.
"""

import asyncio
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

# Ensure event loop exists for ib_insync (needed for Streamlit compatibility)
try:
    asyncio.get_event_loop()
except RuntimeError:
    # Create new event loop if none exists
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from ib_insync import IB, Contract, LimitOrder, MarketOrder, Order, Position, Stock, Trade, util

from core.config_manager import ConfigManager
from core.logging_manager import LoggingManager


class IBKRConnector:
    """
    IBKR connection manager with async support and robust error handling.
    
    Features:
    - Automatic reconnection with exponential backoff
    - Connection health monitoring
    - Market data fetching (real-time and historical)
    - Order management and execution
    - Position and account tracking
    - Python 3.11+ async compatibility
    """
    
    def __init__(self, config: Optional[ConfigManager] = None, logger: Optional[LoggingManager] = None):
        """
        Initialize IBKR connector.
        
        Args:
            config: Configuration manager instance
            logger: Logging manager instance
        """
        self.config = config or ConfigManager()
        self.logger = logger or LoggingManager()
        
        # IBKR configuration
        ibkr_cfg = self.config.config.ibkr
        self.host = ibkr_cfg.host
        self.port = ibkr_cfg.port
        self.client_id = ibkr_cfg.client_id
        self.timeout = ibkr_cfg.timeout
        self.reconnect_attempts = ibkr_cfg.reconnect_attempts
        self.reconnect_delay = ibkr_cfg.reconnect_delay
        
        # Connection state
        self.ib: Optional[IB] = None
        self._connected = False
        self._connection_mode = "PAPER" if self.port == 7497 else "LIVE"
        
        # Persistent background event loop — stays running forever so that
        # run_coroutine_threadsafe() works from any thread (Streamlit, etc.)
        self._loop = self._start_background_loop()

        # Persistent market-data subscriptions: sym -> ib_insync Ticker
        # Kept alive between calls so data streams continuously.
        self._live_subs: Dict[str, Any] = {}
        self._live_contracts: Dict[str, Any] = {}  # sym -> qualified Contract
        
    def _start_background_loop(self) -> asyncio.AbstractEventLoop:
        """Create a new asyncio event loop and run it forever in a daemon thread.

        This is the correct pattern for using ib_insync from synchronous /
        multi-threaded code (e.g. Streamlit).  The loop stays alive for the
        lifetime of the process, so:
          - asyncio.run_coroutine_threadsafe(coro, self._loop) always succeeds
          - ib_insync's reader tasks keep running between calls
          - loop.is_running() is always True
        """
        loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name="ibkr-event-loop", daemon=True)
        t.start()
        return loop

    def _run_async(self, coro, timeout: float = 30):
        """Submit a coroutine to the persistent background loop and block until done.

        Safe to call from any thread — the coroutine always executes on the
        dedicated ibkr-event-loop thread, which is always running.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)
    
    # ==================== Connection Management ====================
    
    async def _connect_async(self) -> bool:
        """Internal async connect method.

        Automatically retries with clientId+1 .. clientId+4 if Gateway rejects
        the initial clientId with Error 326 (already in use).  This handles the
        hot-reload / rapid-restart scenario where the previous connection is still
        registered in Gateway for a brief period.

        Strategy:
          1. connectAsync() — Gateway accepts the TCP connection immediately.
          2. Wait 0.5 s — gives Gateway time to send Error 326 and drop the link.
          3. Check isConnected() — if False, the clientId was rejected; try next.
          4. Repeat for up to MAX_CID_ATTEMPTS sequential clientIds.
        """
        MAX_CID_ATTEMPTS = 5  # will try base, base+1, ..., base+4

        for offset in range(MAX_CID_ATTEMPTS):
            client_id = self.client_id + offset
            try:
                # Clean up any previous IB object before each attempt.
                if self.ib is not None:
                    try:
                        self.ib.disconnect()
                    except Exception:
                        pass
                self.ib = IB()

                await self.ib.connectAsync(
                    self.host,
                    self.port,
                    clientId=client_id,
                    timeout=0,  # Disable internal timeout for Python 3.14+ compatibility
                )

                # Brief pause so Gateway can send Error 326 if clientId is taken.
                await asyncio.sleep(0.5)

                if not self.ib.isConnected():
                    next_id = client_id + 1
                    self.logger.warning(
                        f"clientId {client_id} rejected by Gateway (likely in use). "
                        f"Retrying with clientId {next_id}..."
                    )
                    continue

                # Successfully connected.
                self._connected = True
                accounts = self.ib.managedAccounts()
                self.logger.info(
                    f"✓ Connected to IBKR {self._connection_mode} (clientId={client_id})",
                    extra={
                        "host": self.host,
                        "port": self.port,
                        "accounts": accounts,
                        "mode": self._connection_mode,
                        "client_id": client_id,
                    },
                )
                return True

            except Exception as e:
                self.logger.error(
                    f"IBKR connection attempt failed (clientId={client_id}): {e}",
                    exc_info=True,
                )

        self.logger.error(
            f"Could not connect to IBKR after {MAX_CID_ATTEMPTS} clientId attempts "
            f"({self.client_id}–{self.client_id + MAX_CID_ATTEMPTS - 1})"
        )
        return False
    
    def connect(self) -> bool:
        """
        Connect to IBKR TWS/Gateway.
        
        Returns:
            True if connected successfully
        """
        if self.is_connected():
            self.logger.info("Already connected to IBKR")
            return True
        
        return self._run_async(self._connect_async())
    
    def connect_with_retry(self) -> bool:
        """
        Connect to IBKR with exponential backoff retry logic.
        
        Returns:
            True if connected successfully
        """
        for attempt in range(1, self.reconnect_attempts + 1):
            self.logger.info(f"Connection attempt {attempt}/{self.reconnect_attempts}")
            
            if self.connect():
                return True
            
            if attempt < self.reconnect_attempts:
                delay = self.reconnect_delay * (2 ** (attempt - 1))  # Exponential backoff
                self.logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)
        
        self.logger.error(f"Failed to connect after {self.reconnect_attempts} attempts")
        return False
    
    def disconnect(self):
        """Disconnect from IBKR."""
        if self.ib and self.ib.isConnected():
            # Cancel all persistent market-data subscriptions before disconnecting.
            for sym, contract in list(self._live_contracts.items()):
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass
            self._live_subs.clear()
            self._live_contracts.clear()
            self.ib.disconnect()
            self._connected = False
            self.logger.info("Disconnected from IBKR")
    
    def is_connected(self) -> bool:
        """
        Check if connected to IBKR.
        
        Returns:
            True if connected and healthy
        """
        return self.ib is not None and self.ib.isConnected()
    
    def reconnect(self) -> bool:
        """
        Attempt to reconnect to IBKR.
        
        Returns:
            True if reconnected successfully
        """
        self.logger.warning("Attempting to reconnect...")
        
        if self.is_connected():
            self.disconnect()
        
        time.sleep(self.reconnect_delay)
        return self.connect_with_retry()
    
    # ==================== Account & Position Management ====================
    
    def get_account_value(self, account: str = "") -> float:
        """
        Get account net liquidation value.
        
        Args:
            account: Account code (empty for default)
            
        Returns:
            Net liquidation value in USD
        """
        if not self.is_connected():
            self.logger.error("Not connected to IBKR")
            return 0.0
        
        try:
            account_values = self.ib.accountValues(account)
            
            for av in account_values:
                if av.tag == 'NetLiquidation' and av.currency == 'USD':
                    return float(av.value)
            
            return 0.0
            
        except Exception as e:
            self.logger.error(f"Error getting account value: {e}")
            return 0.0
    
    def get_account_summary(self, account: str = "") -> Dict[str, float]:
        """
        Get comprehensive account summary.
        
        Args:
            account: Account code (empty for default)
            
        Returns:
            Dictionary with account metrics
        """
        if not self.is_connected():
            return {}
        
        try:
            account_values = self.ib.accountValues(account)
            
            summary = {}
            for av in account_values:
                if av.currency == 'USD':
                    key = av.tag.lower().replace(' ', '_')
                    summary[key] = float(av.value) if av.value else 0.0
            
            return summary
            
        except Exception as e:
            self.logger.error(f"Error getting account summary: {e}")
            return {}
    
    def get_positions(self, account: str = "") -> List[Dict[str, Any]]:
        """
        Get current positions.
        
        Args:
            account: Account code (empty for default)
            
        Returns:
            List of position dictionaries
        """
        if not self.is_connected():
            return []
        
        try:
            positions = self.ib.positions(account)
            
            result = []
            for pos in positions:
                result.append({
                    'symbol': pos.contract.symbol,
                    'position': pos.position,
                    'avg_cost': pos.avgCost,
                    'market_price': getattr(pos, 'marketPrice', None),
                    'market_value': getattr(pos, 'marketValue', None),
                    'unrealized_pnl': getattr(pos, 'unrealizedPNL', None),
                    'realized_pnl': getattr(pos, 'realizedPNL', None),
                })
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return []
    
    # ==================== Market Data ====================
    
    def _create_contract(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
        """Create a stock contract."""
        return Stock(symbol, exchange, currency)
    
    async def _qualify_contract_async(self, contract: Contract) -> Optional[Contract]:
        """Qualify a contract asynchronously."""
        try:
            contracts = await self.ib.qualifyContractsAsync(contract)
            return contracts[0] if contracts else None
        except Exception as e:
            self.logger.error(f"Error qualifying contract {contract.symbol}: {e}")
            return None
    
    def get_live_quotes(self, symbols: List[str], wait_secs: float = 2.0) -> Dict[str, Dict]:
        """
        Fetch live tick data for multiple symbols simultaneously via reqMktData.

        Mirrors the TickerMonitor approach from the live dashboard.
        Returns bid, ask, last, high, low, open, close, volume, change_pct, spread
        for each symbol.  Falls back gracefully to empty dict on any error.

        Thread-safe: submits the async work to ib_insync's own background event loop
        via run_coroutine_threadsafe so this can be called from Streamlit's
        ScriptRunner.scriptThread without needing a local event loop.

        Args:
            symbols: List of ticker symbols.
            wait_secs: Seconds to wait for tick data to arrive (default 2).

        Returns:
            Dict mapping symbol -> data dict. Keys present even on failure (empty values).
        """
        if not self.is_connected() or not symbols:
            return {s: {} for s in symbols}

        # self._loop is the persistent background event loop (always running).
        # run_coroutine_threadsafe is safe from any thread.
        future = asyncio.run_coroutine_threadsafe(
            self._async_get_live_quotes(symbols, wait_secs), self._loop
        )
        try:
            return future.result(timeout=wait_secs + 10)
        except Exception as e:
            self.logger.error(f"get_live_quotes failed: {e}", exc_info=True)
            return {s: {} for s in symbols}

    async def _async_get_live_quotes(self, symbols: List[str], wait_secs: float) -> Dict[str, Dict]:
        """Async implementation of get_live_quotes — runs on ib_insync's persistent loop.

        Keeps market-data subscriptions alive between calls (streaming model).
        Only new symbols are qualified (in parallel) and subscribed; existing
        subscriptions are reused.  Symbols no longer in `symbols` are cancelled.
        """
        import math

        symbols_set = set(symbols)
        new_syms    = [s for s in symbols if s not in self._live_subs]
        gone_syms   = [s for s in list(self._live_subs) if s not in symbols_set]

        # ── Cancel subscriptions for removed symbols ──────────────────────────
        for sym in gone_syms:
            try:
                self.ib.cancelMktData(self._live_contracts[sym])
            except Exception:
                pass
            self._live_subs.pop(sym, None)
            self._live_contracts.pop(sym, None)

        # ── Qualify NEW symbols in parallel ───────────────────────────────────
        if new_syms:
            async def _qualify_one(sym: str):
                try:
                    contract = self._create_contract(sym)
                    qualified = await self.ib.qualifyContractsAsync(contract)
                    if qualified:
                        return sym, qualified[0]
                except Exception as e:
                    self.logger.warning(f"Could not qualify {sym}: {e}")
                return sym, None

            results = await asyncio.gather(*[_qualify_one(s) for s in new_syms])
            for sym, qc in results:
                if qc is not None:
                    self._live_contracts[sym] = qc
                    self._live_subs[sym] = self.ib.reqMktData(qc, "", False, False)

            # Wait for the first batch of tick data to arrive.
            await asyncio.sleep(wait_secs)

        # ── Read current values for all active subscriptions ──────────────────
        def _val(v):
            try:
                f = float(v)
                return None if (math.isnan(f) or f == 0.0) else f
            except (TypeError, ValueError):
                return None

        result: Dict[str, Dict] = {}
        for sym in symbols:
            ticker = self._live_subs.get(sym)
            if ticker is None:
                result[sym] = {}
                continue

            last  = _val(ticker.last)
            bid   = _val(ticker.bid)
            ask   = _val(ticker.ask)
            high  = _val(ticker.high)
            low   = _val(ticker.low)
            open_ = _val(ticker.open)
            close = _val(ticker.close)

            try:
                vol = int(ticker.volume) if ticker.volume and not math.isnan(float(ticker.volume)) else None
            except (TypeError, ValueError):
                vol = None

            change_pct = None
            if last and close and close > 0:
                change_pct = round((last - close) / close * 100, 2)

            spread = spread_pct = None
            if bid and ask and ask > bid:
                spread     = round(ask - bid, 4)
                spread_pct = round(spread / last * 100, 3) if last else None

            result[sym] = {
                "last":       last,
                "bid":        bid,
                "ask":        ask,
                "high":       high,
                "low":        low,
                "open":       open_,
                "close":      close,
                "volume":     vol,
                "change_pct": change_pct,
                "spread":     spread,
                "spread_pct": spread_pct,
                "source":     "IBKR",
            }

        return result

    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current market price for a symbol.

        Submits to ib_insync's background event loop so it is safe to call
        from any thread (including Streamlit's ScriptRunner thread).

        Args:
            symbol: Stock ticker symbol

        Returns:
            Current price or None if unavailable
        """
        if not self.is_connected():
            return None

        # self._loop is always running — safe from any thread.
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_get_current_price(symbol), self._loop
            )
            return future.result(timeout=10)
        except Exception as e:
            self.logger.error(f"Error getting price for {symbol}: {e}")
            return None

    async def _async_get_current_price(self, symbol: str) -> Optional[float]:
        """Async impl of get_current_price — must run on ib_insync's event loop."""
        try:
            contract = self._create_contract(symbol)
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                return None
            contract = qualified[0]

            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(1)

            price = ticker.marketPrice()
            self.ib.cancelMktData(contract)

            return float(price) if price and price > 0 else None
        except Exception as e:
            self.logger.error(f"_async_get_current_price({symbol}) error: {e}")
            return None
    
    async def _fetch_historical_async(
        self,
        symbol: str,
        duration: str = "250 D",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True
    ) -> Optional[pd.DataFrame]:
        """Fetch historical data asynchronously."""
        try:
            contract = self._create_contract(symbol)
            contract = await self._qualify_contract_async(contract)
            
            if not contract:
                return None
            
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime='',
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1
            )
            
            if not bars:
                return None
            
            # Convert to DataFrame
            df = util.df(bars)
            
            if df.empty:
                return None
            
            # Standardize column names
            df = df.rename(columns={
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume',
            })
            
            df['Symbol'] = symbol
            df['Date'] = pd.to_datetime(df['date']).dt.date
            df = df.set_index('Date')
            
            return df[['Open', 'High', 'Low', 'Close', 'Volume', 'Symbol']]
            
        except Exception as e:
            self.logger.error(f"Error fetching historical data for {symbol}: {e}")
            return None
    
    def fetch_historical_data(
        self,
        symbol: str,
        duration: str = "250 D",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical market data.
        
        Args:
            symbol: Stock ticker symbol
            duration: Duration string (e.g., "250 D", "1 Y", "5 M")
            bar_size: Bar size (e.g., "1 day", "1 hour", "5 mins")
            what_to_show: Data type (TRADES, MIDPOINT, BID, ASK)
            use_rth: Use regular trading hours only
            
        Returns:
            DataFrame with OHLCV data or None
        """
        if not self.is_connected():
            self.logger.error("Not connected to IBKR")
            return None
        
        return self._run_async(
            self._fetch_historical_async(symbol, duration, bar_size, what_to_show, use_rth)
        )
    
    def fetch_bulk_historical(
        self,
        symbols: List[str],
        duration: str = "250 D",
        delay: float = 0.5
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch historical data for multiple symbols.
        
        Args:
            symbols: List of stock ticker symbols
            duration: Duration string
            delay: Delay between requests (seconds) to avoid rate limits
            
        Returns:
            Dictionary mapping symbol to DataFrame
        """
        if not self.is_connected():
            return {}
        
        data = {}
        
        for i, symbol in enumerate(symbols):
            self.logger.info(f"Fetching {symbol} ({i+1}/{len(symbols)})")
            
            df = self.fetch_historical_data(symbol, duration)
            if df is not None:
                data[symbol] = df
            
            # Rate limiting
            if i < len(symbols) - 1:
                time.sleep(delay)
        
        self.logger.info(f"Successfully fetched data for {len(data)}/{len(symbols)} symbols")
        return data
    
    # ==================== Order Management ====================
    
    def create_market_order(self, action: str, quantity: int) -> MarketOrder:
        """
        Create a market order.
        
        Args:
            action: 'BUY' or 'SELL'
            quantity: Number of shares
            
        Returns:
            MarketOrder object
        """
        return MarketOrder(action, quantity)
    
    def create_limit_order(self, action: str, quantity: int, limit_price: float) -> LimitOrder:
        """
        Create a limit order.
        
        Args:
            action: 'BUY' or 'SELL'
            quantity: Number of shares
            limit_price: Limit price
            
        Returns:
            LimitOrder object
        """
        return LimitOrder(action, quantity, limit_price)
    
    def place_order(self, symbol: str, order: Order) -> Optional[Trade]:
        """
        Place an order.
        
        Args:
            symbol: Stock ticker symbol
            order: Order object (MarketOrder or LimitOrder)
            
        Returns:
            Trade object or None if failed
        """
        if not self.is_connected():
            self.logger.error("Not connected to IBKR")
            return None
        
        try:
            contract = self._create_contract(symbol)
            contract = self._run_async(self._qualify_contract_async(contract))
            
            if not contract:
                return None
            
            trade = self.ib.placeOrder(contract, order)
            
            self.logger.log_trade(
                f"Order placed: {order.action} {order.totalQuantity} {symbol}",
                extra={
                    'symbol': symbol,
                    'action': order.action,
                    'quantity': order.totalQuantity,
                    'order_type': type(order).__name__,
                    'limit_price': getattr(order, 'lmtPrice', None)
                }
            )
            
            return trade
            
        except Exception as e:
            self.logger.error(f"Error placing order for {symbol}: {e}")
            return None
    
    def cancel_order(self, trade: Trade) -> bool:
        """
        Cancel an order.
        
        Args:
            trade: Trade object to cancel
            
        Returns:
            True if cancelled successfully
        """
        if not self.is_connected():
            return False
        
        try:
            self.ib.cancelOrder(trade.order)
            self.logger.info(f"Order cancelled: {trade.contract.symbol}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error cancelling order: {e}")
            return False
    
    def get_open_orders(self) -> List[Trade]:
        """
        Get all open orders.
        
        Returns:
            List of Trade objects
        """
        if not self.is_connected():
            return []
        
        try:
            return self.ib.openTrades()
        except Exception as e:
            self.logger.error(f"Error getting open orders: {e}")
            return []
    
    # ==================== Context Manager ====================
    
    def __enter__(self):
        """Context manager entry."""
        self.connect_with_retry()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()
