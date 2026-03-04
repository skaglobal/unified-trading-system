"""
Test configuration manager
"""
import pytest
from pathlib import Path
from core.config_manager import get_config_manager


def test_config_manager_singleton():
    """Test that config manager is a singleton"""
    config1 = get_config_manager()
    config2 = get_config_manager()
    assert config1 is config2


def test_paper_trading_mode():
    """Test paper trading mode detection"""
    config = get_config_manager()
    # Default should be paper trading
    assert config.is_paper_trading() is True


def test_ibkr_params():
    """Test IBKR parameter retrieval"""
    config = get_config_manager()
    params = config.get_ibkr_params()
    
    assert 'host' in params
    assert 'port' in params
    assert 'clientId' in params
    assert isinstance(params['port'], int)
    assert isinstance(params['clientId'], int)


def test_config_structure():
    """Test config structure"""
    config = get_config_manager()
    
    # Check main sections exist
    assert hasattr(config.config, 'ibkr')
    assert hasattr(config.config, 'trading')
    assert hasattr(config.config, 'data_sources')
    assert hasattr(config.config, 'risk')


def test_risk_config():
    """Test risk configuration"""
    config = get_config_manager()
    risk = config.config.risk
    
    assert hasattr(risk, 'max_position_size_pct')
    assert hasattr(risk, 'max_portfolio_risk_pct')
    assert hasattr(risk, 'max_daily_loss_pct')
    
    # Check reasonable values
    assert 0 < risk.max_position_size_pct <= 100
    assert 0 < risk.max_portfolio_risk_pct <= 100
