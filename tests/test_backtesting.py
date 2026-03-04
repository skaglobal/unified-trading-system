"""
Test backtesting engine
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from backtesting.backtest_engine import BacktestEngine, Trade, BacktestResult
from strategies.base_strategy import BaseStrategy


class SimpleTestStrategy(BaseStrategy):
    """Simple test strategy for backtesting"""
    
    def __init__(self):
        super().__init__("TestStrategy")
    
    def generate_signals(self, data):
        """Generate simple buy/sell signals"""
        signals = []
        
        for symbol, df in data.items():
            if df.empty or len(df) < 2:
                continue
            
            # Simple logic: buy if price up 2 days, sell if down 2 days
            if len(df) >= 3:
                last_3 = df.tail(3)
                closes = last_3['close'].values
                
                if closes[-1] > closes[-2] > closes[-3]:
                    signals.append({
                        'symbol': symbol,
                        'action': 'buy',
                        'price': closes[-1],
                        'reason': 'uptrend'
                    })
                elif closes[-1] < closes[-2] < closes[-3]:
                    signals.append({
                        'symbol': symbol,
                        'action': 'sell',
                        'price': closes[-1],
                        'reason': 'downtrend'
                    })
        
        return signals


@pytest.fixture
def sample_market_data():
    """Create sample market data for backtesting"""
    dates = pd.date_range(start='2024-01-01', periods=100, freq='D')
    
    data = {}
    
    # Create uptrending stock
    data['UPSTOCK'] = pd.DataFrame({
        'date': dates,
        'open': 100 + np.arange(100) * 0.5 + np.random.randn(100) * 0.5,
        'high': 102 + np.arange(100) * 0.5 + np.random.randn(100) * 0.5,
        'low': 98 + np.arange(100) * 0.5 + np.random.randn(100) * 0.5,
        'close': 100 + np.arange(100) * 0.5 + np.random.randn(100) * 0.5,
        'volume': np.random.randint(1000000, 10000000, 100)
    })
    
    # Create downtrending stock
    data['DOWNSTOCK'] = pd.DataFrame({
        'date': dates,
        'open': 200 - np.arange(100) * 0.3 + np.random.randn(100) * 0.5,
        'high': 202 - np.arange(100) * 0.3 + np.random.randn(100) * 0.5,
        'low': 198 - np.arange(100) * 0.3 + np.random.randn(100) * 0.5,
        'close': 200 - np.arange(100) * 0.3 + np.random.randn(100) * 0.5,
        'volume': np.random.randint(1000000, 10000000, 100)
    })
    
    # Ensure high >= low
    for symbol in data:
        df = data[symbol]
        df['high'] = df[['high', 'low', 'close', 'open']].max(axis=1)
        df['low'] = df[['low', 'close', 'open']].min(axis=1)
    
    return data


def test_backtest_engine_initialization():
    """Test backtest engine initialization"""
    engine = BacktestEngine(initial_capital=100000)
    
    assert engine.initial_capital == 100000
    assert engine.capital == 100000
    assert len(engine.positions) == 0
    assert len(engine.closed_trades) == 0


def test_backtest_run(sample_market_data):
    """Test running a backtest"""
    engine = BacktestEngine(initial_capital=100000)
    strategy = SimpleTestStrategy()
    
    result = engine.run(strategy, sample_market_data)
    
    assert isinstance(result, BacktestResult)
    assert result.strategy_name == "TestStrategy"
    assert result.initial_capital == 100000


def test_trade_creation():
    """Test trade object creation"""
    trade = Trade(
        symbol='AAPL',
        entry_date=datetime(2024, 1, 1),
        entry_price=150.0,
        shares=100,
        direction='long'
    )
    
    assert trade.symbol == 'AAPL'
    assert trade.shares == 100
    assert trade.status == 'open'
    assert trade.pnl == 0.0


def test_trade_close():
    """Test closing a trade"""
    trade = Trade(
        symbol='AAPL',
        entry_date=datetime(2024, 1, 1),
        entry_price=150.0,
        shares=100,
        direction='long'
    )
    
    trade.close(datetime(2024, 1, 10), 160.0, "signal")
    
    assert trade.exit_price == 160.0
    assert trade.status == "signal"
    assert trade.pnl == 1000.0  # (160 - 150) * 100
    assert trade.pnl_percent > 0


def test_backtest_result_metrics():
    """Test backtest result metrics calculation"""
    result = BacktestResult(
        strategy_name="Test",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 12, 31),
        initial_capital=100000,
        final_capital=110000,
        total_return=10000,
        total_return_pct=10.0
    )
    
    # Add some trades
    winning_trade = Trade('AAPL', datetime(2024, 1, 1), 100, shares=100)
    winning_trade.close(datetime(2024, 1, 10), 110, "signal")
    
    losing_trade = Trade('GOOGL', datetime(2024, 2, 1), 200, shares=50)
    losing_trade.close(datetime(2024, 2, 10), 190, "signal")
    
    result.trades = [winning_trade, losing_trade]
    result.calculate_metrics()
    
    assert result.total_trades == 2
    assert result.winning_trades == 1
    assert result.losing_trades == 1
    assert result.win_rate == 50.0


def test_backtest_with_no_data():
    """Test backtest with empty data"""
    engine = BacktestEngine(initial_capital=100000)
    strategy = SimpleTestStrategy()
    
    result = engine.run(strategy, {})
    
    assert result.total_trades == 0
    assert result.final_capital == result.initial_capital


def test_trade_short_position():
    """Test short position P&L calculation"""
    trade = Trade(
        symbol='AAPL',
        entry_date=datetime(2024, 1, 1),
        entry_price=150.0,
        shares=100,
        direction='short'
    )
    
    # Close at lower price (profit for short)
    trade.close(datetime(2024, 1, 10), 140.0, "signal")
    
    assert trade.pnl == 1000.0  # (150 - 140) * 100
    assert trade.pnl_percent > 0
