"""
Test indicators
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from analysis.indicators import (
    calculate_rsi, calculate_sma, calculate_ema,
    calculate_macd, calculate_bollinger_bands, calculate_atr
)


@pytest.fixture
def sample_data():
    """Create sample OHLCV data"""
    dates = pd.date_range(start='2024-01-01', periods=100, freq='D')
    data = pd.DataFrame({
        'date': dates,
        'open': 100 + np.random.randn(100).cumsum(),
        'high': 102 + np.random.randn(100).cumsum(),
        'low': 98 + np.random.randn(100).cumsum(),
        'close': 100 + np.random.randn(100).cumsum(),
        'volume': np.random.randint(1000000, 10000000, 100)
    })
    # Ensure high >= low
    data['high'] = data[['high', 'low', 'close', 'open']].max(axis=1)
    data['low'] = data[['low', 'close', 'open']].min(axis=1)
    return data


def test_rsi_calculation(sample_data):
    """Test RSI calculation"""
    result = calculate_rsi(sample_data, period=14)
    
    assert 'RSI_14' in result.columns
    assert not result['RSI_14'].isna().all()
    
    # RSI should be between 0 and 100
    valid_rsi = result['RSI_14'].dropna()
    assert (valid_rsi >= 0).all() and (valid_rsi <= 100).all()


def test_sma_calculation(sample_data):
    """Test SMA calculation"""
    result = calculate_sma(sample_data, period=20)
    
    assert 'SMA_20' in result.columns
    
    # First SMA value should appear at period position
    valid_sma = result['SMA_20'].dropna()
    assert len(valid_sma) > 0


def test_ema_calculation(sample_data):
    """Test EMA calculation"""
    result = calculate_ema(sample_data, period=12)
    
    assert 'EMA_12' in result.columns
    
    valid_ema = result['EMA_12'].dropna()
    assert len(valid_ema) > 0


def test_macd_calculation(sample_data):
    """Test MACD calculation"""
    result = calculate_macd(sample_data)
    
    assert 'MACD' in result.columns
    assert 'MACD_SIGNAL' in result.columns
    assert 'MACD_HIST' in result.columns
    
    # Check histogram is difference
    valid_rows = result[['MACD', 'MACD_SIGNAL', 'MACD_HIST']].dropna()
    if len(valid_rows) > 0:
        hist_check = valid_rows['MACD'] - valid_rows['MACD_SIGNAL']
        assert np.allclose(valid_rows['MACD_HIST'], hist_check, atol=0.01)


def test_bollinger_bands_calculation(sample_data):
    """Test Bollinger Bands calculation"""
    result = calculate_bollinger_bands(sample_data, period=20, std_dev=2)
    
    assert 'BB_MIDDLE' in result.columns
    assert 'BB_UPPER' in result.columns
    assert 'BB_LOWER' in result.columns
    
    # Upper should be > middle > lower
    valid_rows = result[['BB_UPPER', 'BB_MIDDLE', 'BB_LOWER']].dropna()
    if len(valid_rows) > 0:
        assert (valid_rows['BB_UPPER'] >= valid_rows['BB_MIDDLE']).all()
        assert (valid_rows['BB_MIDDLE'] >= valid_rows['BB_LOWER']).all()


def test_atr_calculation(sample_data):
    """Test ATR calculation"""
    result = calculate_atr(sample_data, period=14)
    
    assert 'ATR_14' in result.columns
    
    # ATR should be positive
    valid_atr = result['ATR_14'].dropna()
    assert (valid_atr > 0).all()


def test_empty_dataframe():
    """Test indicators with empty dataframe"""
    empty_df = pd.DataFrame()
    
    result = calculate_rsi(empty_df)
    assert result.empty
    
    result = calculate_sma(empty_df)
    assert result.empty


def test_insufficient_data():
    """Test indicators with insufficient data"""
    short_data = pd.DataFrame({
        'close': [100, 101, 102],
        'high': [101, 102, 103],
        'low': [99, 100, 101]
    })
    
    # Should not crash, but may have NaN values
    result = calculate_rsi(short_data, period=14)
    assert 'RSI_14' in result.columns
