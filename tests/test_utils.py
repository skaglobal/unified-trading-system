"""
Test core utilities
"""
import pytest
from datetime import datetime, time
import pytz

from core.utils import (
    is_market_open, calculate_position_size, RateLimiter,
    format_currency, safe_divide, validate_symbol
)


def test_market_hours_weekday():
    """Test market hours on weekday"""
    # Create a datetime during market hours (EST)
    est = pytz.timezone('US/Eastern')
    market_open = est.localize(datetime(2024, 1, 15, 10, 0))  # Monday 10 AM
    
    assert is_market_open(market_open) is True
    
    # Before market
    before_market = est.localize(datetime(2024, 1, 15, 8, 0))  # Monday 8 AM
    assert is_market_open(before_market) is False
    
    # After market
    after_market = est.localize(datetime(2024, 1, 15, 17, 0))  # Monday 5 PM
    assert is_market_open(after_market) is False


def test_market_hours_weekend():
    """Test market closed on weekend"""
    est = pytz.timezone('US/Eastern')
    saturday = est.localize(datetime(2024, 1, 13, 10, 0))  # Saturday
    
    assert is_market_open(saturday) is False


def test_position_size_calculation():
    """Test position size calculation"""
    size = calculate_position_size(
        capital=100000,
        entry_price=100,
        position_pct=2.0
    )
    
    # 2% of 100000 = 2000, at $100/share = 20 shares
    assert size == 20


def test_position_size_edge_cases():
    """Test position size edge cases"""
    # Zero capital
    assert calculate_position_size(0, 100, 2.0) == 0
    
    # Zero price
    assert calculate_position_size(100000, 0, 2.0) == 0
    
    # Negative values
    assert calculate_position_size(-100000, 100, 2.0) == 0
    assert calculate_position_size(100000, -100, 2.0) == 0


def test_rate_limiter():
    """Test rate limiter"""
    import time
    
    limiter = RateLimiter(max_calls=2, period=1.0)
    
    # First call should succeed immediately
    start = time.time()
    limiter.wait_if_needed()
    elapsed = time.time() - start
    assert elapsed < 0.1
    
    # Second call should succeed immediately
    limiter.wait_if_needed()
    elapsed = time.time() - start
    assert elapsed < 0.1
    
    # Third call should wait
    limiter.wait_if_needed()
    elapsed = time.time() - start
    assert elapsed >= 0.9  # Should have waited ~1 second


def test_format_currency():
    """Test currency formatting"""
    assert format_currency(1234.56) == "$1,234.56"
    assert format_currency(1234567.89) == "$1,234,567.89"
    assert format_currency(-1234.56) == "-$1,234.56"
    assert format_currency(0) == "$0.00"


def test_safe_divide():
    """Test safe division"""
    assert safe_divide(10, 2) == 5.0
    assert safe_divide(10, 0) == 0.0
    assert safe_divide(10, 0, default=None) is None
    assert safe_divide(0, 10) == 0.0


def test_validate_symbol():
    """Test symbol validation"""
    assert validate_symbol("AAPL") is True
    assert validate_symbol("GOOGL") is True
    assert validate_symbol("SPY") is True
    
    # Invalid symbols
    assert validate_symbol("") is False
    assert validate_symbol("123") is False
    assert validate_symbol("TOOLONGSYMBOL") is False
    assert validate_symbol("AA PL") is False  # Space
    assert validate_symbol("AA-PL") is False  # Dash (for this simple validator)


def test_validate_symbol_case():
    """Test symbol validation handles case"""
    assert validate_symbol("aapl") is True  # Should accept lowercase
    assert validate_symbol("AaPl") is True  # Mixed case
