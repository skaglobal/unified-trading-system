"""
Technical Indicators - Comprehensive collection of trading indicators.

Includes trend, momentum, volatility, and volume indicators.
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple

from core.logging_manager import LoggingManager


class TechnicalIndicators:
    """
    Calculate technical indicators for trading analysis.
    
    Supports:
    - Trend: MA, EMA, VWAP
    - Momentum: RSI, MACD, Stochastic
    - Volatility: ATR, Bollinger Bands
    - Volume: OBV, Volume SMA
    """
    
    def __init__(self, logger: Optional[LoggingManager] = None):
        """Initialize indicator calculator."""
        self.logger = logger or LoggingManager()
    
    # ==================== Moving Averages ====================
    
    @staticmethod
    def sma(data: pd.Series, period: int) -> pd.Series:
        """Simple Moving Average."""
        return data.rolling(window=period).mean()
    
    @staticmethod
    def ema(data: pd.Series, period: int) -> pd.Series:
        """Exponential Moving Average."""
        return data.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        """Volume Weighted Average Price."""
        typical_price = (df['High'] + df['Low'] + df['Close']) / 3
        return (typical_price * df['Volume']).cumsum() / df['Volume'].cumsum()
    
    def add_moving_averages(
        self,
        df: pd.DataFrame,
        sma_periods: list = [20, 50, 200],
        ema_periods: list = [9, 21]
    ) -> pd.DataFrame:
        """Add multiple moving averages."""
        df = df.copy()
        
        for period in sma_periods:
            df[f'SMA_{period}'] = self.sma(df['Close'], period)
        
        for period in ema_periods:
            df[f'EMA_{period}'] = self.ema(df['Close'], period)
        
        return df
    
    # ==================== Volatility Indicators ====================
    
    def atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Average True Range."""
        high = df['High']
        low = df['Low']
        close = df['Close'].shift(1)
        
        tr1 = high - low
        tr2 = abs(high - close)
        tr3 = abs(low - close)
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()
    
    def bollinger_bands(
        self,
        df: pd.DataFrame,
        period: int = 20,
        std_dev: float = 2.0
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Bollinger Bands.
        
        Returns:
            Tuple of (upper_band, middle_band, lower_band)
        """
        middle = self.sma(df['Close'], period)
        std = df['Close'].rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        
        return upper, middle, lower
    
    def add_volatility_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add volatility indicators."""
        df = df.copy()
        
        # ATR
        df['ATR_14'] = self.atr(df, 14)
        df['ATR_20'] = self.atr(df, 20)
        
        # Bollinger Bands
        upper, middle, lower = self.bollinger_bands(df)
        df['BB_Upper'] = upper
        df['BB_Middle'] = middle
        df['BB_Lower'] = lower
        df['BB_Width'] = (upper - lower) / middle * 100
        
        # Historical Volatility (20-day)
        returns = df['Close'].pct_change()
        df['HV_20'] = returns.rolling(window=20).std() * np.sqrt(252) * 100
        
        return df
    
    # ==================== Momentum Indicators ====================
    
    def rsi(self, data: pd.Series, period: int = 14) -> pd.Series:
        """Relative Strength Index."""
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def macd(
        self,
        data: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        MACD (Moving Average Convergence Divergence).
        
        Returns:
            Tuple of (macd_line, signal_line, histogram)
        """
        ema_fast = self.ema(data, fast)
        ema_slow = self.ema(data, slow)
        
        macd_line = ema_fast - ema_slow
        signal_line = self.ema(macd_line, signal)
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    def stochastic(
        self,
        df: pd.DataFrame,
        k_period: int = 14,
        d_period: int = 3
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Stochastic Oscillator.
        
        Returns:
            Tuple of (%K, %D)
        """
        low_min = df['Low'].rolling(window=k_period).min()
        high_max = df['High'].rolling(window=k_period).max()
        
        k = 100 * (df['Close'] - low_min) / (high_max - low_min)
        d = k.rolling(window=d_period).mean()
        
        return k, d
    
    def add_momentum_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add momentum indicators."""
        df = df.copy()
        
        # RSI
        df['RSI_14'] = self.rsi(df['Close'], 14)
        
        # MACD
        macd_line, signal_line, histogram = self.macd(df['Close'])
        df['MACD'] = macd_line
        df['MACD_Signal'] = signal_line
        df['MACD_Hist'] = histogram
        
        # Stochastic
        k, d = self.stochastic(df)
        df['Stoch_K'] = k
        df['Stoch_D'] = d
        
        # ROC (Rate of Change)
        df['ROC_10'] = df['Close'].pct_change(10) * 100
        
        return df
    
    # ==================== Volume Indicators ====================
    
    def obv(self, df: pd.DataFrame) -> pd.Series:
        """On-Balance Volume."""
        obv = pd.Series(index=df.index, dtype=float)
        obv.iloc[0] = 0
        
        for i in range(1, len(df)):
            if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
                obv.iloc[i] = obv.iloc[i-1] + df['Volume'].iloc[i]
            elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
                obv.iloc[i] = obv.iloc[i-1] - df['Volume'].iloc[i]
            else:
                obv.iloc[i] = obv.iloc[i-1]
        
        return obv
    
    def add_volume_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add volume indicators."""
        df = df.copy()
        
        # Volume SMA
        df['Volume_SMA_20'] = self.sma(df['Volume'], 20)
        
        # Volume Ratio
        df['Volume_Ratio'] = df['Volume'] / df['Volume_SMA_20']
        
        # OBV
        df['OBV'] = self.obv(df)
        
        # VWAP
        if 'High' in df.columns and 'Low' in df.columns:
            df['VWAP'] = self.vwap(df)
        
        return df
    
    # ==================== Individual Indicator Methods ====================
    
    def add_rsi(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """Add RSI indicator."""
        df = df.copy()
        df[f'RSI_{period}'] = self.rsi(df['Close'], period)
        return df
    
    def add_macd(self, df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
        """Add MACD indicator."""
        df = df.copy()
        macd_line, signal_line, histogram = self.macd(df['Close'], fast, slow, signal)
        df['MACD'] = macd_line
        df['MACD_Signal'] = signal_line  
        df['MACD_Hist'] = histogram
        return df
    
    def add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """Add ATR indicator."""
        df = df.copy()
        df[f'ATR_{period}'] = self.atr(df, period)
        return df
    
    def add_bollinger_bands(self, df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
        """Add Bollinger Bands."""
        df = df.copy()
        upper, middle, lower = self.bollinger_bands(df, period, std_dev)
        df['BB_Upper'] = upper
        df['BB_Middle'] = middle
        df['BB_Lower'] = lower
        df['BB_Width'] = (upper - lower) / middle * 100
        return df
    
    # ==================== Combined Analysis ====================
    
    def add_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add all technical indicators to DataFrame."""
        df = df.copy()
        
        # Moving Averages
        df = self.add_moving_averages(df)
        
        # Volatility
        df = self.add_volatility_indicators(df)
        
        # Momentum
        df = self.add_momentum_indicators(df)
        
        # Volume
        df = self.add_volume_indicators(df)
        
        # Additional derived indicators
        df = self._add_derived_indicators(df)
        
        return df
    
    def _add_derived_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add derived indicators and signals."""
        df = df.copy()
        
        # Price position within Bollinger Bands
        if 'BB_Upper' in df.columns and 'BB_Lower' in df.columns:
            bb_range = df['BB_Upper'] - df['BB_Lower']
            df['BB_Position'] = (df['Close'] - df['BB_Lower']) / bb_range * 100
        
        # MA crossovers (golden cross / death cross)
        if 'SMA_50' in df.columns and 'SMA_200' in df.columns:
            df['Golden_Cross'] = (df['SMA_50'] > df['SMA_200']).astype(int)
            df['Death_Cross'] = (df['SMA_50'] < df['SMA_200']).astype(int)
        
        # Price above/below MAs
        if 'SMA_20' in df.columns:
            df['Above_SMA_20'] = (df['Close'] > df['SMA_20']).astype(int)
        if 'SMA_50' in df.columns:
            df['Above_SMA_50'] = (df['Close'] > df['SMA_50']).astype(int)
        if 'SMA_200' in df.columns:
            df['Above_SMA_200'] = (df['Close'] > df['SMA_200']).astype(int)
        
        # Trend strength (all MAs aligned)
        if all(col in df.columns for col in ['SMA_20', 'SMA_50', 'SMA_200']):
            df['Bullish_Alignment'] = (
                (df['SMA_20'] > df['SMA_50']) & 
                (df['SMA_50'] > df['SMA_200'])
            ).astype(int)
            
            df['Bearish_Alignment'] = (
                (df['SMA_20'] < df['SMA_50']) & 
                (df['SMA_50'] < df['SMA_200'])
            ).astype(int)
        
        # RSI conditions
        if 'RSI_14' in df.columns:
            df['RSI_Oversold'] = (df['RSI_14'] < 30).astype(int)
            df['RSI_Overbought'] = (df['RSI_14'] > 70).astype(int)
        
        # MACD bullish/bearish
        if 'MACD' in df.columns and 'MACD_Signal' in df.columns:
            df['MACD_Bullish'] = (df['MACD'] > df['MACD_Signal']).astype(int)
            df['MACD_Bearish'] = (df['MACD'] < df['MACD_Signal']).astype(int)
        
        return df
    
    # ==================== Signal Detection ====================
    
    def detect_breakout(
        self,
        df: pd.DataFrame,
        lookback: int = 20,
        volume_multiple: float = 1.5
    ) -> bool:
        """
        Detect breakout pattern.
        
        Args:
            df: Price DataFrame
            lookback: Consolidation lookback period
            volume_multiple: Required volume multiple for confirmation
            
        Returns:
            True if breakout detected
        """
        if len(df) < lookback + 1:
            return False
        
        recent_high = df['High'].iloc[-lookback-1:-1].max()
        current_close = df['Close'].iloc[-1]
        current_volume = df['Volume'].iloc[-1]
        avg_volume = df['Volume'].iloc[-lookback-1:-1].mean()
        
        # Price breakout above consolidation high with volume
        return (
            current_close > recent_high and
            current_volume > avg_volume * volume_multiple
        )
    
    def detect_pullback(
        self,
        df: pd.DataFrame,
        ma_period: int = 20,
        max_distance_pct: float = 3.0
    ) -> bool:
        """
        Detect pullback to moving average.
        
        Args:
            df: Price DataFrame
            ma_period: MA period to check
            max_distance_pct: Maximum distance % from MA
            
        Returns:
            True if pullback detected
        """
        if len(df) < ma_period + 1:
            return False
        
        ma_col = f'SMA_{ma_period}'
        if ma_col not in df.columns:
            df = self.add_moving_averages(df, sma_periods=[ma_period])
        
        current_price = df['Close'].iloc[-1]
        ma_value = df[ma_col].iloc[-1]
        
        if ma_value == 0 or pd.isna(ma_value):
            return False
        
        distance_pct = abs((current_price - ma_value) / ma_value * 100)
        
        return distance_pct <= max_distance_pct
    
    def calculate_trend_strength(self, df: pd.DataFrame) -> float:
        """
        Calculate overall trend strength (0-100).
        
        Combines multiple indicators for trend assessment.
        """
        if len(df) < 200:
            return 0.0
        
        score = 0.0
        factors = 0
        
        # MA alignment (40% weight)
        if all(col in df.columns for col in ['SMA_20', 'SMA_50', 'SMA_200']):
            if df['Bullish_Alignment'].iloc[-1] == 1:
                score += 40
            elif df['Bearish_Alignment'].iloc[-1] == 1:
                score -= 40
            factors += 1
        
        # RSI (20% weight)
        if 'RSI_14' in df.columns:
            rsi = df['RSI_14'].iloc[-1]
            if 40 <= rsi <= 60:
                score += 20  # Neutral momentum
            elif rsi > 50:
                score += 10  # Bullish momentum
            factors += 1
        
        # MACD (20% weight)
        if 'MACD_Bullish' in df.columns:
            if df['MACD_Bullish'].iloc[-1] == 1:
                score += 20
            elif df['MACD_Bearish'].iloc[-1] == 1:
                score -= 20
            factors += 1
        
        # Volume confirmation (20% weight)
        if 'Volume_Ratio' in df.columns:
            vol_ratio = df['Volume_Ratio'].iloc[-1]
            if vol_ratio > 1.2:
                score += 20
            factors += 1
        
        return abs(score) if factors > 0 else 0.0


# ==================== Convenience Functions ====================
# Standalone function wrappers for easy imports

_indicator_instance = TechnicalIndicators()

_COL_MAP = {
    'open': 'Open', 'high': 'High', 'low': 'Low',
    'close': 'Close', 'volume': 'Volume', 'date': 'Date',
}
_COL_MAP_REVERSE = {v: k for k, v in _COL_MAP.items()}


def _normalize_cols(df: pd.DataFrame) -> tuple:
    """Temporarily uppercase OHLCV columns so TechnicalIndicators works.
    Returns (normalized_df, {renamed_cols}) where renamed_cols maps new→old."""
    renames = {k: v for k, v in _COL_MAP.items() if k in df.columns}
    if renames:
        df = df.rename(columns=renames)
    return df, renames


def _restore_cols(df: pd.DataFrame, renames: dict) -> pd.DataFrame:
    """Restore lowercase column names after indicator calculation."""
    reverse = {v: k for k, v in renames.items()}
    return df.rename(columns=reverse)


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate RSI indicator."""
    df, renames = _normalize_cols(df)
    result = _indicator_instance.add_rsi(df, period)
    return _restore_cols(result, renames)


def calculate_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Calculate Simple Moving Average."""
    df, renames = _normalize_cols(df)
    result = _indicator_instance.add_moving_averages(df, sma_periods=[period])
    return _restore_cols(result, renames)


def calculate_ema(df: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    """Calculate Exponential Moving Average."""
    df, renames = _normalize_cols(df)
    result = _indicator_instance.add_moving_averages(df, ema_periods=[period])
    return _restore_cols(result, renames)


def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Calculate MACD indicator."""
    df, renames = _normalize_cols(df)
    result = _indicator_instance.add_macd(df, fast, slow, signal)
    return _restore_cols(result, renames)


def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Calculate Bollinger Bands."""
    df, renames = _normalize_cols(df)
    result = _indicator_instance.add_bollinger_bands(df, period, std_dev)
    return _restore_cols(result, renames)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate Average True Range."""
    df, renames = _normalize_cols(df)
    result = _indicator_instance.add_atr(df, period)
    return _restore_cols(result, renames)
