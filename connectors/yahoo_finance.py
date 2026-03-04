"""
Yahoo Finance Connector - Free market data fetching using yfinance.

Provides real-time quotes, historical data, and fundamental information.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from core.config_manager import ConfigManager
from core.logging_manager import LoggingManager
from core.utils import RateLimiter


class YahooFinanceConnector:
    """
    Yahoo Finance data connector using yfinance library.
    
    Features:
    - Real-time and delayed quotes
    - Historical OHLCV data
    - Company fundamentals
    - Bulk data fetching with rate limiting
    - Error handling and retry logic
    """
    
    def __init__(self, config: Optional[ConfigManager] = None, logger: Optional[LoggingManager] = None):
        """
        Initialize Yahoo Finance connector.
        
        Args:
            config: Configuration manager instance
            logger: Logging manager instance
        """
        self.config = config or ConfigManager()
        self.logger = logger or LoggingManager()
        
        # Rate limiter: 2000 requests per hour = ~33 per minute
        self.rate_limiter = RateLimiter(max_calls=30, period=60)
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current market price.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            Current price or None
        """
        try:
            self.rate_limiter.wait_if_needed()
            
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            # Try multiple price fields
            price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
            
            if price:
                return float(price)
            
            # Fallback: get from recent history
            hist = ticker.history(period='1d')
            if not hist.empty:
                return float(hist['Close'].iloc[-1])
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    def get_quote(self, symbol: str) -> Dict:
        """
        Get comprehensive quote data.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            Dictionary with quote data
        """
        try:
            self.rate_limiter.wait_if_needed()
            
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            return {
                'symbol': symbol,
                'price': info.get('currentPrice') or info.get('regularMarketPrice'),
                'previous_close': info.get('previousClose'),
                'open': info.get('regularMarketOpen'),
                'day_high': info.get('dayHigh'),
                'day_low': info.get('dayLow'),
                'volume': info.get('volume'),
                'avg_volume': info.get('averageVolume'),
                'market_cap': info.get('marketCap'),
                'pe_ratio': info.get('trailingPE'),
                'eps': info.get('trailingEps'),
                'dividend_yield': info.get('dividendYield'),
                '52w_high': info.get('fiftyTwoWeekHigh'),
                '52w_low': info.get('fiftyTwoWeekLow'),
                'beta': info.get('beta'),
            }
            
        except Exception as e:
            self.logger.error(f"Error getting quote for {symbol}: {e}")
            return {'symbol': symbol}
    
    def fetch_historical_data(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical OHLCV data.
        
        Args:
            symbol: Stock ticker symbol
            period: Data period (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max)
            interval: Data interval (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo)
            start: Start date (alternative to period)
            end: End date (alternative to period)
            
        Returns:
            DataFrame with OHLCV data or None
        """
        try:
            self.rate_limiter.wait_if_needed()
            
            ticker = yf.Ticker(symbol)
            
            if start and end:
                df = ticker.history(start=start, end=end, interval=interval)
            else:
                df = ticker.history(period=period, interval=interval)
            
            if df.empty:
                self.logger.warning(f"No data returned for {symbol}")
                return None
            
            # Standardize column names
            df = df.rename(columns={
                'Open': 'Open',
                'High': 'High',
                'Low': 'Low',
                'Close': 'Close',
                'Volume': 'Volume',
            })
            
            df['Symbol'] = symbol
            
            # Clean data
            df = df.dropna(subset=['Close'])
            
            return df[['Open', 'High', 'Low', 'Close', 'Volume', 'Symbol']]
            
        except Exception as e:
            self.logger.error(f"Error fetching historical data for {symbol}: {e}")
            return None
    
    def fetch_bulk_historical(
        self,
        symbols: List[str],
        period: str = "1y",
        interval: str = "1d"
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch historical data for multiple symbols efficiently.
        
        Args:
            symbols: List of stock ticker symbols
            period: Data period
            interval: Data interval
            
        Returns:
            Dictionary mapping symbol to DataFrame
        """
        data = {}
        
        # Use bulk download for efficiency
        try:
            self.logger.info(f"Bulk downloading data for {len(symbols)} symbols...")
            
            # yfinance supports bulk download
            bulk_df = yf.download(
                symbols,
                period=period,
                interval=interval,
                group_by='ticker',
                threads=True,
                progress=False
            )
            
            if bulk_df.empty:
                self.logger.warning("Bulk download returned no data")
                return data
            
            # Split by symbol
            for symbol in symbols:
                try:
                    if len(symbols) == 1:
                        df = bulk_df.copy()
                    else:
                        df = bulk_df[symbol].copy()
                    
                    if not df.empty:
                        # Standardize columns
                        df = df.rename(columns={
                            'Open': 'Open',
                            'High': 'High',
                            'Low': 'Low',
                            'Close': 'Close',
                            'Volume': 'Volume',
                        })
                        df['Symbol'] = symbol
                        df = df.dropna(subset=['Close'])
                        
                        if not df.empty:
                            data[symbol] = df[['Open', 'High', 'Low', 'Close', 'Volume', 'Symbol']]
                    
                except Exception as e:
                    self.logger.warning(f"Error processing {symbol}: {e}")
            
            self.logger.info(f"Successfully downloaded data for {len(data)}/{len(symbols)} symbols")
            
        except Exception as e:
            self.logger.error(f"Bulk download error: {e}")
            
            # Fallback: individual downloads
            self.logger.info("Falling back to individual downloads...")
            for symbol in symbols:
                df = self.fetch_historical_data(symbol, period, interval)
                if df is not None:
                    data[symbol] = df
        
        return data
    
    def get_company_info(self, symbol: str) -> Dict:
        """
        Get company fundamental information.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            Dictionary with company info
        """
        try:
            self.rate_limiter.wait_if_needed()
            
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            return {
                'symbol': symbol,
                'name': info.get('longName') or info.get('shortName'),
                'sector': info.get('sector'),
                'industry': info.get('industry'),
                'country': info.get('country'),
                'website': info.get('website'),
                'description': info.get('longBusinessSummary'),
                'employees': info.get('fullTimeEmployees'),
                'market_cap': info.get('marketCap'),
                'enterprise_value': info.get('enterpriseValue'),
                'trailing_pe': info.get('trailingPE'),
                'forward_pe': info.get('forwardPE'),
                'peg_ratio': info.get('pegRatio'),
                'price_to_book': info.get('priceToBook'),
                'profit_margin': info.get('profitMargins'),
                'revenue_growth': info.get('revenueGrowth'),
                'return_on_equity': info.get('returnOnEquity'),
                'debt_to_equity': info.get('debtToEquity'),
                'current_ratio': info.get('currentRatio'),
                'recommendation': info.get('recommendationKey'),
                'target_price': info.get('targetMeanPrice'),
            }
            
        except Exception as e:
            self.logger.error(f"Error getting company info for {symbol}: {e}")
            return {'symbol': symbol}
    
    def get_financial_statements(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """
        Get financial statements.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            Dictionary with income statement, balance sheet, and cash flow
        """
        try:
            self.rate_limiter.wait_if_needed()
            
            ticker = yf.Ticker(symbol)
            
            return {
                'income_statement': ticker.financials,
                'balance_sheet': ticker.balance_sheet,
                'cash_flow': ticker.cashflow,
            }
            
        except Exception as e:
            self.logger.error(f"Error getting financials for {symbol}: {e}")
            return {}
    
    def validate_symbols(self, symbols: List[str]) -> List[str]:
        """
        Validate that symbols exist and return valid ones.
        
        Args:
            symbols: List of ticker symbols to validate
            
        Returns:
            List of valid symbols
        """
        valid = []
        
        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                info = ticker.info
                
                # Check if symbol is valid (has basic info)
                if info.get('regularMarketPrice') or info.get('previousClose'):
                    valid.append(symbol)
                else:
                    self.logger.warning(f"Invalid symbol: {symbol}")
                    
            except Exception:
                self.logger.warning(f"Could not validate symbol: {symbol}")
        
        return valid
    
    def search_symbols(self, query: str) -> List[Dict]:
        """
        Search for symbols matching a query.
        
        Note: This is a limited implementation as yfinance doesn't have built-in search.
        
        Args:
            query: Search query
            
        Returns:
            List of matching symbols with info
        """
        # This is a placeholder - yfinance doesn't support search
        # In production, you might want to use a different API for symbol search
        self.logger.warning("Symbol search not fully implemented with yfinance")
        return []
