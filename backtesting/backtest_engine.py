"""
Backtesting Engine
Simulates strategy performance on historical data
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from core.logging_manager import get_logger
from core.config_manager import get_config_manager

logger = get_logger("backtesting.engine")


@dataclass
class Trade:
    """Represents a single trade"""
    symbol: str
    entry_date: datetime
    entry_price: float
    exit_date: Optional[datetime] = None
    exit_price: Optional[float] = None
    shares: int = 0
    direction: str = "long"  # long or short
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    pnl: float = 0.0
    pnl_percent: float = 0.0
    status: str = "open"  # open, closed, stopped
    
    def close(self, exit_date: datetime, exit_price: float, reason: str = "signal"):
        """Close the trade"""
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.status = reason
        
        if self.direction == "long":
            self.pnl = (exit_price - self.entry_price) * self.shares
            self.pnl_percent = ((exit_price - self.entry_price) / self.entry_price) * 100
        else:
            self.pnl = (self.entry_price - exit_price) * self.shares
            self.pnl_percent = ((self.entry_price - exit_price) / self.entry_price) * 100


@dataclass
class BacktestResult:
    """Results of a backtest run"""
    strategy_name: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    
    # Performance metrics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    
    def calculate_metrics(self):
        """Calculate performance metrics from trades"""
        if not self.trades:
            return
        
        closed_trades = [t for t in self.trades if t.status in ['closed', 'stopped']]
        self.total_trades = len(closed_trades)
        
        if self.total_trades == 0:
            return
        
        wins = [t for t in closed_trades if t.pnl > 0]
        losses = [t for t in closed_trades if t.pnl <= 0]
        
        self.winning_trades = len(wins)
        self.losing_trades = len(losses)
        self.win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        
        self.avg_win = np.mean([t.pnl for t in wins]) if wins else 0
        self.avg_loss = np.mean([t.pnl for t in losses]) if losses else 0
        
        total_wins = sum(t.pnl for t in wins)
        total_losses = abs(sum(t.pnl for t in losses))
        self.profit_factor = (total_wins / total_losses) if total_losses > 0 else 0
        
        # Calculate max drawdown from equity curve
        if not self.equity_curve.empty and 'equity' in self.equity_curve.columns:
            equity = self.equity_curve['equity'].values
            running_max = np.maximum.accumulate(equity)
            drawdown = equity - running_max
            self.max_drawdown = abs(drawdown.min())
            self.max_drawdown_pct = (self.max_drawdown / running_max[np.argmin(drawdown)] * 100) if running_max[np.argmin(drawdown)] > 0 else 0
        
        # Calculate Sharpe ratio (simplified - assuming daily returns)
        if not self.equity_curve.empty and 'equity' in self.equity_curve.columns:
            returns = self.equity_curve['equity'].pct_change().dropna()
            if len(returns) > 1 and returns.std() > 0:
                self.sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252)  # Annualized


class BacktestEngine:
    """Main backtesting engine"""
    
    def __init__(self, initial_capital: float = 100000):
        self.config = get_config_manager()
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions: Dict[str, Trade] = {}
        self.closed_trades: List[Trade] = []
        self.equity_history: List[Tuple[datetime, float]] = []
        
    def reset(self):
        """Reset engine state"""
        self.capital = self.initial_capital
        self.positions = {}
        self.closed_trades = []
        self.equity_history = []
        
    def run(self, 
            strategy,
            data: Dict[str, pd.DataFrame],
            start_date: Optional[datetime] = None,
            end_date: Optional[datetime] = None) -> BacktestResult:
        """
        Run backtest
        
        Args:
            strategy: Strategy instance with generate_signals method
            data: Dict of symbol -> DataFrame with OHLCV data
            start_date: Start date for backtest
            end_date: End date for backtest
            
        Returns:
            BacktestResult with performance metrics
        """
        self.reset()
        
        # Get date range
        all_dates = set()
        for df in data.values():
            if 'date' in df.columns:
                all_dates.update(df['date'].values)
            elif df.index.name == 'date' or isinstance(df.index, pd.DatetimeIndex):
                all_dates.update(df.index.values)
        
        if not all_dates:
            logger.error("No dates found in data")
            return self._create_empty_result(strategy.__class__.__name__)
        
        all_dates = sorted(list(all_dates))
        
        if start_date:
            all_dates = [d for d in all_dates if pd.Timestamp(d) >= pd.Timestamp(start_date)]
        if end_date:
            all_dates = [d for d in all_dates if pd.Timestamp(d) <= pd.Timestamp(end_date)]
        
        if not all_dates:
            logger.error("No dates in specified range")
            return self._create_empty_result(strategy.__class__.__name__)
        
        actual_start = pd.Timestamp(all_dates[0])
        actual_end = pd.Timestamp(all_dates[-1])
        
        logger.info(f"Starting backtest from {actual_start} to {actual_end}")
        
        # Process each date
        for current_date in all_dates:
            current_date = pd.Timestamp(current_date)
            
            # Get data up to current date for each symbol
            current_data = {}
            for symbol, df in data.items():
                if 'date' in df.columns:
                    mask = df['date'] <= current_date
                    current_data[symbol] = df[mask].copy()
                elif isinstance(df.index, pd.DatetimeIndex):
                    mask = df.index <= current_date
                    current_data[symbol] = df[mask].copy()
            
            # Check stops and targets on existing positions
            self._check_exits(current_data, current_date)
            
            # Generate new signals
            try:
                signals = strategy.generate_signals(current_data)
                
                # Process signals
                for signal in signals:
                    if signal['action'] == 'buy' and signal['symbol'] not in self.positions:
                        self._enter_position(signal, current_data, current_date)
                    elif signal['action'] == 'sell' and signal['symbol'] in self.positions:
                        self._exit_position(signal['symbol'], current_data, current_date, "signal")
            except Exception as e:
                logger.error(f"Error generating signals for {current_date}: {e}")
                continue
            
            # Record equity
            self._record_equity(current_data, current_date)
        
        # Close any remaining positions at end
        for symbol in list(self.positions.keys()):
            self._exit_position(symbol, data, actual_end, "end")
        
        # Create result
        result = self._create_result(strategy.__class__.__name__, actual_start, actual_end)
        
        logger.info(f"Backtest complete: {result.total_trades} trades, "
                   f"Win rate: {result.win_rate:.1f}%, "
                   f"Total return: {result.total_return_pct:.2f}%")
        
        return result
    
    def _enter_position(self, signal: Dict, data: Dict[str, pd.DataFrame], date: datetime):
        """Enter a new position"""
        symbol = signal['symbol']
        
        if symbol in self.positions:
            return
        
        # Get current price
        if symbol not in data or data[symbol].empty:
            return
        
        current_bar = data[symbol].iloc[-1]
        entry_price = current_bar.get('close', current_bar.get('Close', 0))
        
        if entry_price <= 0:
            return
        
        # Calculate position size (simple: 2% of capital per trade)
        position_size_pct = 0.02
        position_value = self.capital * position_size_pct
        shares = int(position_value / entry_price)
        
        if shares == 0:
            return
        
        cost = shares * entry_price
        
        if cost > self.capital:
            return
        
        # Create trade
        trade = Trade(
            symbol=symbol,
            entry_date=date,
            entry_price=entry_price,
            shares=shares,
            direction=signal.get('direction', 'long'),
            stop_loss=signal.get('stop_loss'),
            target=signal.get('target')
        )
        
        self.positions[symbol] = trade
        self.capital -= cost
        
        logger.debug(f"Entered {symbol} @ ${entry_price:.2f} x {shares} shares")
    
    def _exit_position(self, symbol: str, data: Dict[str, pd.DataFrame], 
                       date: datetime, reason: str = "signal"):
        """Exit an existing position"""
        if symbol not in self.positions:
            return
        
        trade = self.positions[symbol]
        
        # Get exit price
        if symbol not in data or data[symbol].empty:
            return
        
        current_bar = data[symbol].iloc[-1]
        exit_price = current_bar.get('close', current_bar.get('Close', 0))
        
        if exit_price <= 0:
            return
        
        # Close trade
        trade.close(date, exit_price, reason)
        
        # Update capital
        self.capital += (trade.shares * exit_price)
        
        # Move to closed trades
        self.closed_trades.append(trade)
        del self.positions[symbol]
        
        logger.debug(f"Exited {symbol} @ ${exit_price:.2f}, P&L: ${trade.pnl:.2f} ({trade.pnl_percent:.2f}%)")
    
    def _check_exits(self, data: Dict[str, pd.DataFrame], date: datetime):
        """Check stop losses and targets"""
        for symbol in list(self.positions.keys()):
            if symbol not in data or data[symbol].empty:
                continue
            
            trade = self.positions[symbol]
            current_bar = data[symbol].iloc[-1]
            
            low = current_bar.get('low', current_bar.get('Low', 0))
            high = current_bar.get('high', current_bar.get('High', float('inf')))
            
            # Check stop loss
            if trade.stop_loss and low <= trade.stop_loss:
                self._exit_position(symbol, data, date, "stopped")
                continue
            
            # Check target
            if trade.target and high >= trade.target:
                self._exit_position(symbol, data, date, "target")
    
    def _record_equity(self, data: Dict[str, pd.DataFrame], date: datetime):
        """Record current equity value"""
        position_value = 0
        
        for symbol, trade in self.positions.items():
            if symbol in data and not data[symbol].empty:
                current_price = data[symbol].iloc[-1].get('close', 
                                                          data[symbol].iloc[-1].get('Close', 0))
                position_value += trade.shares * current_price
        
        equity = self.capital + position_value
        self.equity_history.append((date, equity))
    
    def _create_result(self, strategy_name: str, start_date: datetime, 
                       end_date: datetime) -> BacktestResult:
        """Create backtest result from trades"""
        final_capital = self.capital + sum(
            t.shares * t.entry_price for t in self.positions.values()
        )
        
        result = BacktestResult(
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return=final_capital - self.initial_capital,
            total_return_pct=((final_capital - self.initial_capital) / self.initial_capital * 100),
            trades=self.closed_trades.copy()
        )
        
        # Create equity curve DataFrame
        if self.equity_history:
            result.equity_curve = pd.DataFrame(
                self.equity_history,
                columns=['date', 'equity']
            )
        
        # Calculate metrics
        result.calculate_metrics()
        
        return result
    
    def _create_empty_result(self, strategy_name: str) -> BacktestResult:
        """Create empty result for failed backtest"""
        return BacktestResult(
            strategy_name=strategy_name,
            start_date=datetime.now(),
            end_date=datetime.now(),
            initial_capital=self.initial_capital,
            final_capital=self.initial_capital,
            total_return=0,
            total_return_pct=0
        )
