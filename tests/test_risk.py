"""
Test risk manager
"""
import pytest
from datetime import datetime
import pandas as pd

from risk.risk_manager import RiskManager, calculate_position_size


def test_risk_manager_initialization():
    """Test risk manager initialization"""
    risk_mgr = RiskManager(max_position_pct=2.0, max_portfolio_risk_pct=10.0)
    
    assert risk_mgr.max_position_pct == 2.0
    assert risk_mgr.max_portfolio_risk_pct == 10.0


def test_calculate_position_size_basic():
    """Test basic position size calculation"""
    account_value = 100000
    risk_per_trade_pct = 1.0
    entry_price = 100
    stop_loss = 95
    
    shares = calculate_position_size(
        account_value=account_value,
        risk_per_trade_pct=risk_per_trade_pct,
        entry_price=entry_price,
        stop_loss=stop_loss
    )
    
    # Risk = 100000 * 0.01 = 1000
    # Risk per share = 100 - 95 = 5
    # Shares = 1000 / 5 = 200
    assert shares == 200


def test_calculate_position_size_zero_risk():
    """Test position size with zero risk (stop at entry)"""
    shares = calculate_position_size(
        account_value=100000,
        risk_per_trade_pct=1.0,
        entry_price=100,
        stop_loss=100  # No risk
    )
    
    # Should return 0 or minimal position
    assert shares == 0


def test_calculate_position_size_negative():
    """Test position size with invalid stop (above entry)"""
    shares = calculate_position_size(
        account_value=100000,
        risk_per_trade_pct=1.0,
        entry_price=100,
        stop_loss=105  # Stop above entry
    )
    
    assert shares == 0


def test_risk_manager_portfolio_limit():
    """Test portfolio risk limit check"""
    risk_mgr = RiskManager(max_portfolio_risk_pct=10.0)
    
    account_value = 100000
    current_risk = 5000  # 5% at risk
    
    # Should allow more risk
    assert risk_mgr.can_take_more_risk(account_value, current_risk) is True
    
    # Now at limit
    current_risk = 10000  # 10% at risk
    assert risk_mgr.can_take_more_risk(account_value, current_risk) is False
    
    # Exceeds limit
    current_risk = 15000  # 15% at risk
    assert risk_mgr.can_take_more_risk(account_value, current_risk) is False


def test_risk_manager_position_count():
    """Test maximum position count"""
    risk_mgr = RiskManager(max_positions=10)
    
    assert risk_mgr.can_open_position(current_positions=5) is True
    assert risk_mgr.can_open_position(current_positions=10) is False
    assert risk_mgr.can_open_position(current_positions=15) is False


def test_calculate_risk_amount():
    """Test risk amount calculation"""
    risk_mgr = RiskManager()
    
    entry_price = 100
    stop_loss = 95
    shares = 100
    
    risk_amount = risk_mgr.calculate_risk_amount(entry_price, stop_loss, shares)
    
    # Risk = (100 - 95) * 100 = 500
    assert risk_amount == 500


def test_atr_position_sizing():
    """Test ATR-based position sizing"""
    risk_mgr = RiskManager()
    
    account_value = 100000
    risk_per_trade_pct = 1.0
    entry_price = 100
    atr = 2.0  # ATR of 2
    atr_multiplier = 2.0  # Stop at 2x ATR
    
    stop_distance = atr * atr_multiplier  # 4
    stop_loss = entry_price - stop_distance  # 96
    
    shares = calculate_position_size(
        account_value=account_value,
        risk_per_trade_pct=risk_per_trade_pct,
        entry_price=entry_price,
        stop_loss=stop_loss
    )
    
    # Risk = 1000, Risk per share = 4, Shares = 250
    assert shares == 250
