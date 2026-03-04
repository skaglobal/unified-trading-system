"""
Order Execution Engine - Order management and execution with paper/live modes.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from connectors.ibkr_connector import IBKRConnector
from risk.risk_manager import RiskManager
from strategies.base_strategy import SignalType, TradingSignal
from core.config_manager import ConfigManager
from core.logging_manager import LoggingManager


class OrderStatus(Enum):
    """Order status types."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class ExecutionMode(Enum):
    """Execution modes."""
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderExecutor:
    """
    Execute trading orders with risk management.
    
    Supports:
    - Paper trading simulation
    - Live order execution via IBKR
    - Order tracking and management
    - Automatic risk enforcement
    """
    
    def __init__(
        self,
        mode: ExecutionMode = ExecutionMode.PAPER,
        ibkr: Optional[IBKRConnector] = None,
        risk_manager: Optional[RiskManager] = None,
        config: Optional[ConfigManager] = None,
        logger: Optional[LoggingManager] = None
    ):
        """
        Initialize order executor.
        
        Args:
            mode: Execution mode (PAPER or LIVE)
            ibkr: IBKR connector (required for LIVE mode)
            risk_manager: Risk manager instance
            config: Configuration manager
            logger: Logging manager
        """
        self.mode = mode
        self.ibkr = ibkr
        self.risk_manager = risk_manager or RiskManager()
        self.config = config or ConfigManager()
        self.logger = logger or LoggingManager()
        
        # Order tracking
        self.orders: Dict[str, Dict] = {}
        self.next_order_id = 1
        
        # Paper trading state
        self.paper_cash = 100000.0  # Starting paper cash
        self.paper_positions: Dict[str, Dict] = {}
        
        # Validate configuration
        if mode == ExecutionMode.LIVE and ibkr is None:
            raise ValueError("IBKR connector required for LIVE mode")
    
    def execute_signal(
        self,
        signal: TradingSignal,
        portfolio_value: float
    ) -> Optional[str]:
        """
        Execute a trading signal.
        
        Args:
            signal: Trading signal to execute
            portfolio_value: Current portfolio value
            
        Returns:
            Order ID if successful, None otherwise
        """
        # Check risk limits
        can_trade, reason = self.risk_manager.can_open_position(
            signal,
            portfolio_value
        )
        
        if not can_trade:
            self.logger.warning(
                f"Signal rejected for {signal.symbol}: {reason}",
                extra={'symbol': signal.symbol, 'reason': reason}
            )
            return None
        
        # Calculate position size
        quantity = self.risk_manager.calculate_position_size(
            signal,
            portfolio_value,
            risk_per_trade_pct=1.0
        )
        
        if quantity <= 0:
            self.logger.warning(f"Invalid quantity for {signal.symbol}")
            return None
        
        # Execute based on mode
        if self.mode == ExecutionMode.PAPER:
            return self._execute_paper(signal, quantity)
        else:
            return self._execute_live(signal, quantity)
    
    def _execute_paper(self, signal: TradingSignal, quantity: int) -> str:
        """Execute order in paper trading mode."""
        order_id = f"PAPER_{self.next_order_id}"
        self.next_order_id += 1
        
        # Calculate order value
        order_value = signal.price * quantity
        
        # Check paper cash
        if signal.signal_type == SignalType.BUY:
            if order_value > self.paper_cash:
                self.logger.warning(
                    f"Insufficient paper cash for {signal.symbol}",
                    extra={
                        'required': order_value,
                        'available': self.paper_cash
                    }
                )
                return None
            
            # Deduct cash
            self.paper_cash -= order_value
            
            # Add position
            self.paper_positions[signal.symbol] = {
                'quantity': quantity,
                'entry_price': signal.price,
                'stop_loss': signal.stop_loss,
                'take_profit': signal.take_profit
            }
            
            # Update risk manager
            self.risk_manager.add_position(signal, quantity)
        
        # Record order
        self.orders[order_id] = {
            'order_id': order_id,
            'symbol': signal.symbol,
            'signal_type': signal.signal_type,
            'quantity': quantity,
            'price': signal.price,
            'status': OrderStatus.FILLED,
            'timestamp': datetime.now(),
            'mode': 'PAPER'
        }
        
        self.logger.log_trade(
            f"PAPER: {'BUY' if signal.signal_type == SignalType.BUY else 'SELL'} {quantity} {signal.symbol} @ ${signal.price:.2f}",
            extra={
                'order_id': order_id,
                'symbol': signal.symbol,
                'action': signal.signal_type.value,
                'quantity': quantity,
                'price': signal.price,
                'mode': 'PAPER'
            }
        )
        
        return order_id
    
    def _execute_live(self, signal: TradingSignal, quantity: int) -> Optional[str]:
        """Execute order in live trading mode."""
        if not self.ibkr or not self.ibkr.is_connected():
            self.logger.error("IBKR not connected")
            return None
        
        try:
            # Create order
            action = 'BUY' if signal.signal_type == SignalType.BUY else 'SELL'
            
            # Use market order for immediate execution
            # In production, you might want limit orders
            order = self.ibkr.create_market_order(action, quantity)
            
            # Place order
            trade = self.ibkr.place_order(signal.symbol, order)
            
            if trade is None:
                self.logger.error(f"Failed to place order for {signal.symbol}")
                return None
            
            order_id = str(trade.order.orderId)
            
            # Record order
            self.orders[order_id] = {
                'order_id': order_id,
                'symbol': signal.symbol,
                'signal_type': signal.signal_type,
                'quantity': quantity,
                'price': signal.price,
                'status': OrderStatus.SUBMITTED,
                'timestamp': datetime.now(),
                'mode': 'LIVE',
                'trade': trade
            }
            
            # Add to risk manager (will update when filled)
            if signal.signal_type == SignalType.BUY:
                self.risk_manager.add_position(signal, quantity)
            
            self.logger.log_trade(
                f"LIVE: Order submitted for {signal.symbol}",
                extra={
                    'order_id': order_id,
                    'symbol': signal.symbol,
                    'action': action,
                    'quantity': quantity,
                    'mode': 'LIVE'
                }
            )
            
            return order_id
            
        except Exception as e:
            self.logger.error(f"Error executing live order: {e}", exc_info=True)
            return None
    
    def close_position(
        self,
        symbol: str,
        reason: str = "Manual close"
    ) -> bool:
        """
        Close an open position.
        
        Args:
            symbol: Stock symbol
            reason: Reason for closing
            
        Returns:
            True if successful
        """
        if self.mode == ExecutionMode.PAPER:
            return self._close_paper_position(symbol, reason)
        else:
            return self._close_live_position(symbol, reason)
    
    def _close_paper_position(self, symbol: str, reason: str) -> bool:
        """Close paper trading position."""
        if symbol not in self.paper_positions:
            self.logger.warning(f"No paper position for {symbol}")
            return False
        
        pos = self.paper_positions[symbol]
        
        # Simulate current price (in production, get from market)
        exit_price = pos['entry_price'] * 1.02  # Assume 2% profit for simulation
        
        # Calculate proceeds
        proceeds = exit_price * pos['quantity']
        self.paper_cash += proceeds
        
        # Update risk manager
        pnl = self.risk_manager.remove_position(symbol, exit_price)
        
        # Remove position
        del self.paper_positions[symbol]
        
        self.logger.log_trade(
            f"PAPER: Position closed for {symbol}",
            extra={
                'symbol': symbol,
                'quantity': pos['quantity'],
                'entry_price': pos['entry_price'],
                'exit_price': exit_price,
                'pnl': pnl,
                'reason': reason
            }
        )
        
        return True
    
    def _close_live_position(self, symbol: str, reason: str) -> bool:
        """Close live trading position."""
        if not self.ibkr or not self.ibkr.is_connected():
            self.logger.error("IBKR not connected")
            return False
        
        try:
            # Get current position
            positions = self.ibkr.get_positions()
            pos = next((p for p in positions if p['symbol'] == symbol), None)
            
            if pos is None:
                self.logger.warning(f"No live position for {symbol}")
                return False
            
            # Create sell order
            quantity = abs(pos['position'])
            order = self.ibkr.create_market_order('SELL', quantity)
            
            # Place order
            trade = self.ibkr.place_order(symbol, order)
            
            if trade:
                self.logger.log_trade(
                    f"LIVE: Close order submitted for {symbol}",
                    extra={
                        'symbol': symbol,
                        'quantity': quantity,
                        'reason': reason
                    }
                )
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error closing live position: {e}", exc_info=True)
            return False
    
    def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        if self.mode == ExecutionMode.PAPER:
            return [
                {
                    'symbol': symbol,
                    **pos,
                    'mode': 'PAPER'
                }
                for symbol, pos in self.paper_positions.items()
            ]
        else:
            if self.ibkr and self.ibkr.is_connected():
                positions = self.ibkr.get_positions()
                for pos in positions:
                    pos['mode'] = 'LIVE'
                return positions
            return []
    
    def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """Get status of an order."""
        if order_id not in self.orders:
            return None
        
        return self.orders[order_id]['status']
    
    def get_performance_summary(self) -> Dict:
        """Get execution performance summary."""
        return {
            'mode': self.mode.value,
            'total_orders': len(self.orders),
            'open_positions': len(self.get_open_positions()),
            'paper_cash': self.paper_cash if self.mode == ExecutionMode.PAPER else None,
            'risk_summary': self.risk_manager.get_portfolio_summary(
                self.paper_cash if self.mode == ExecutionMode.PAPER else 100000.0
            )
        }
