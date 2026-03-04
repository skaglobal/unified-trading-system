"""
Finviz Elite Scraper - Web scraping for Finviz screener data.

Supports both cookie-based and credential-based authentication.
"""

import os
import re
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

import pandas as pd
import requests

from core.config_manager import ConfigManager
from core.logging_manager import LoggingManager


class FinvizScraper:
    """
    Finviz Elite screener data fetcher with robust authentication.
    
    Features:
    - Cookie-based authentication (recommended)
    - Credential-based authentication (best-effort)
    - CSV export with data normalization
    - Rate limiting and retry logic
    - Support for custom screener URLs
    """
    
    def __init__(self, config: Optional[ConfigManager] = None, logger: Optional[LoggingManager] = None):
        """
        Initialize Finviz scraper.
        
        Args:
            config: Configuration manager instance
            logger: Logging manager instance
        """
        self.config = config or ConfigManager()
        self.logger = logger or LoggingManager()
        
        # Finviz configuration
        self.base_url = "https://finviz.com"
        self.export_url = f"{self.base_url}/export.ashx"
        
        # Authentication - prefer environment variables
        self.cookie = os.environ.get("FINVIZ_COOKIE", "")
        self.username = os.environ.get("FINVIZ_USER", "")
        self.password = os.environ.get("FINVIZ_PASS", "")
        
        # Screener configuration
        self.view = 152  # Elite view with all columns
        self.stocks_filter = "exch_nasd,exch_nyse,exch_amex,cap_500mup"
        self.etfs_filter = "ind_etf,avgvol_1mup"
        
        # Rate limiting
        self.request_delay = 2.0  # seconds between requests
        self.max_retries = 4
        
        # Session
        self.session = self._build_session()
    
    def _build_session(self) -> requests.Session:
        """Build requests session with authentication."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36"
            ),
            "Accept": "text/csv,application/octet-stream,*/*;q=0.8",
        })
        
        # Cookie-based auth (recommended)
        if self.cookie:
            self.logger.info("Using cookie-based Finviz authentication")
            session.headers.update({"Cookie": self.cookie})
            return session
        
        # Credential-based auth (best-effort)
        if self.username and self.password:
            self.logger.info("Attempting credential-based Finviz authentication")
            self._login_with_credentials(session)
            return session
        
        self.logger.warning("No Finviz authentication provided - may be rate-limited")
        return session
    
    def _login_with_credentials(self, session: requests.Session) -> bool:
        """
        Attempt to log in with credentials.
        
        Args:
            session: Requests session
            
        Returns:
            True if login successful
        """
        login_endpoints = ["/login.ashx", "/login"]
        
        for endpoint in login_endpoints:
            url = self.base_url + endpoint
            try:
                resp = session.post(
                    url,
                    data={
                        "email": self.username,
                        "password": self.password,
                        "remember": "on",
                    },
                    timeout=20
                )
                
                if resp.status_code in (200, 302):
                    # Verify session
                    home = session.get(f"{self.base_url}/", timeout=20)
                    if home.status_code == 200:
                        self.logger.info("Finviz login successful")
                        return True
                        
            except requests.RequestException as e:
                self.logger.warning(f"Login attempt via {endpoint} failed: {e}")
        
        self.logger.error("Credential login failed - consider using FINVIZ_COOKIE")
        return False
    
    def _to_export_url(self, screener_url: str) -> str:
        """Convert a screener URL to an export URL."""
        parsed = urlparse(screener_url)
        new_path = parsed.path.replace("screener.ashx", "export.ashx")
        return urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment))
    
    def _fetch_csv_with_retry(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetch CSV text with retry logic.
        
        Args:
            url: Screener URL
            
        Returns:
            Tuple of (csv_text, error_message)
        """
        export_url = self._to_export_url(url)
        delay = self.request_delay
        
        for attempt in range(1, self.max_retries + 1):
            try:
                self.logger.info(f"Fetching Finviz CSV (attempt {attempt}/{self.max_retries})")
                
                response = self.session.get(
                    export_url,
                    timeout=60,
                    headers={"Referer": url},
                    allow_redirects=True
                )
                
                content_type = response.headers.get("Content-Type", "")
                
                # Check if we got CSV
                if response.status_code == 200 and ("text/csv" in content_type.lower() or "," in response.text):
                    return response.text, None
                
                # Check for HTML (auth failure)
                if "text/html" in content_type.lower():
                    self.logger.error("Received HTML instead of CSV - authentication may have failed")
                    return None, "Authentication failed"
                
                self.logger.warning(
                    f"Unexpected response: status={response.status_code}, "
                    f"content_type={content_type}, length={len(response.text)}"
                )
                
            except requests.RequestException as e:
                self.logger.warning(f"Request error: {e}")
            
            # Exponential backoff
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
        
        return None, f"Failed to fetch after {self.max_retries} attempts"
    
    def _normalize_value(self, value: str, column: str) -> Optional[float]:
        """
        Normalize scraped values to floats.
        
        Args:
            value: Raw value string
            column: Column name for type detection
            
        Returns:
            Normalized float value or None
        """
        if not value or value.strip() in ("-", "N/A", "na", "n/a", ""):
            return None
        
        s = value.strip().replace(",", "").upper()
        
        # Percentage values
        if "%" in s or column.lower() in ["change", "perf"]:
            s = s.replace("%", "")
            try:
                return float(s)
            except ValueError:
                return None
        
        # Values with suffixes (K, M, B, T)
        mult = 1.0
        if s.endswith("K"):
            mult, s = 1e3, s[:-1]
        elif s.endswith("M"):
            mult, s = 1e6, s[:-1]
        elif s.endswith("B"):
            mult, s = 1e9, s[:-1]
        elif s.endswith("T"):
            mult, s = 1e12, s[:-1]
        
        # Remove $ signs
        s = s.replace("$", "")
        
        try:
            return float(s) * mult
        except ValueError:
            return None
    
    def _normalize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize DataFrame columns.
        
        Args:
            df: Raw DataFrame from Finviz
            
        Returns:
            Normalized DataFrame
        """
        # Standardize column names
        column_map = {col.lower().strip(): col for col in df.columns}
        
        def get_column(*names):
            for name in names:
                if name.lower() in column_map:
                    return column_map[name.lower()]
            return None
        
        # Rename key columns
        rename_dict = {}
        ticker_col = get_column("ticker", "symbol")
        if ticker_col:
            rename_dict[ticker_col] = "Ticker"
        
        price_col = get_column("price", "last")
        if price_col:
            rename_dict[price_col] = "Price"
        
        volume_col = get_column("volume", "avg volume", "average volume")
        if volume_col:
            rename_dict[volume_col] = "Volume"
        
        mktcap_col = get_column("market cap", "mkt cap")
        if mktcap_col:
            rename_dict[mktcap_col] = "Market_Cap"
        
        change_col = get_column("change", "chg")
        if change_col:
            rename_dict[change_col] = "Change_Pct"
        
        if rename_dict:
            df = df.rename(columns=rename_dict)
        
        # Normalize numeric columns
        numeric_columns = ["Price", "Volume", "Market_Cap", "Change_Pct"]
        for col in numeric_columns:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: self._normalize_value(str(x), col))
        
        return df
    
    def fetch_screener(self, filter_string: str) -> Optional[pd.DataFrame]:
        """
        Fetch data for a custom filter.
        
        Args:
            filter_string: Finviz filter string (e.g., "exch_nasd,cap_500mup")
            
        Returns:
            DataFrame with screener results or None
        """
        params = {
            "v": str(self.view),
            "f": filter_string,
        }
        url = f"{self.base_url}/screener.ashx?{urlencode(params)}"
        
        csv_text, error = self._fetch_csv_with_retry(url)
        
        if error:
            self.logger.error(f"Failed to fetch screener: {error}")
            return None
        
        if not csv_text:
            self.logger.error("Empty CSV response")
            return None
        
        try:
            df = pd.read_csv(StringIO(csv_text), on_bad_lines="skip")
            
            if df.empty:
                self.logger.warning("Empty DataFrame from Finviz")
                return None
            
            # Normalize data
            df = self._normalize_dataframe(df)
            
            self.logger.info(f"Fetched {len(df)} rows from Finviz")
            return df
            
        except Exception as e:
            self.logger.error(f"Error parsing Finviz CSV: {e}")
            return None
    
    def fetch_stocks(self, min_market_cap: float = 500e6, min_volume: int = 500000) -> Optional[pd.DataFrame]:
        """
        Fetch stock universe.
        
        Args:
            min_market_cap: Minimum market cap
            min_volume: Minimum average volume
            
        Returns:
            DataFrame with stock data
        """
        # Build filter
        filters = ["exch_nasd", "exch_nyse", "exch_amex"]
        
        if min_market_cap >= 1e9:
            filters.append(f"cap_{int(min_market_cap/1e6)}mup")
        
        filter_string = ",".join(filters)
        
        df = self.fetch_screener(filter_string)
        
        if df is not None and not df.empty:
            # Additional filtering
            if "Volume" in df.columns and min_volume > 0:
                df = df[df["Volume"] >= min_volume]
            
            df["Type"] = "stock"
        
        return df
    
    def fetch_etfs(self, min_volume: int = 1000000) -> Optional[pd.DataFrame]:
        """
        Fetch ETF universe.
        
        Args:
            min_volume: Minimum average volume
            
        Returns:
            DataFrame with ETF data
        """
        filter_string = "ind_etf"
        
        df = self.fetch_screener(filter_string)
        
        if df is not None and not df.empty:
            if "Volume" in df.columns and min_volume > 0:
                df = df[df["Volume"] >= min_volume]
            
            df["Type"] = "etf"
        
        return df
    
    def fetch_universe(self, include_stocks: bool = True, include_etfs: bool = False) -> pd.DataFrame:
        """
        Fetch complete trading universe.
        
        Args:
            include_stocks: Include stocks
            include_etfs: Include ETFs
            
        Returns:
            Combined DataFrame
        """
        dfs = []
        
        if include_stocks:
            stocks_df = self.fetch_stocks()
            if stocks_df is not None and not stocks_df.empty:
                dfs.append(stocks_df)
        
        if include_etfs:
            etfs_df = self.fetch_etfs()
            if etfs_df is not None and not etfs_df.empty:
                dfs.append(etfs_df)
        
        if not dfs:
            self.logger.error("Failed to fetch any universe data")
            return pd.DataFrame()
        
        # Combine and deduplicate
        combined = pd.concat(dfs, ignore_index=True)
        if "Ticker" in combined.columns:
            combined = combined.drop_duplicates(subset=["Ticker"], keep="first")
        
        self.logger.info(f"Fetched universe: {len(combined)} symbols")
        return combined
    
    def fetch_custom_screener_url(self, url: str) -> Optional[pd.DataFrame]:
        """
        Fetch data from a custom Finviz screener URL.
        
        Args:
            url: Full Finviz screener URL
            
        Returns:
            DataFrame with results
        """
        csv_text, error = self._fetch_csv_with_retry(url)
        
        if error:
            self.logger.error(f"Failed to fetch custom screener: {error}")
            return None
        
        if not csv_text:
            return None
        
        try:
            df = pd.read_csv(StringIO(csv_text), on_bad_lines="skip")
            df = self._normalize_dataframe(df)
            return df
        except Exception as e:
            self.logger.error(f"Error parsing custom screener: {e}")
            return None
