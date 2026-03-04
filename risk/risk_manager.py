"""
Risk Manager - Portfolio-level risk management and position sizing.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from strategies.base_strategy import TradingSignal
from core.config_manager import ConfigManager
from core.logging_manager import LoggingManager


@dataclass
class RiskLimits:
    """Risk limit configuration."""
    max_portfolio_risk_pct: float = 2.0  # Max % of portfolio at risk
    max_position_size_pct: float = 10.0  # Max % per position
    max_sector_exposure_pct: float = 30.0  # Max % per sector
    max_correlation: float = 0.7  # Max correlation between positions
    max_daily_loss_pct: float = 5.0  # Max daily loss %
    max_positions: int = 10  # Max concurrent positions


@dataclass
class Position:
    """Active trading position."""
    symbol: str
    quantity: int
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: Optional[float]
    unrealized_pnl: float
    unrealized_pnl_pct: float


class RiskManager:
    """
    Portfolio risk management.
    
    Features:
    - Position sizing based on risk
    - Portfolio heat management
    - Sector exposure limits
    - Correlation monitoring
    - Daily loss limits
    """
    
    def __init__(
        self,
        limits: Optional[RiskLimits] = None,
        config: Optional[ConfigManager] = None,
        logger: Optional[LoggingManager] = None,
        max_position_pct: Optional[float] = None,
        max_portfolio_risk_pct: Optional[float] = None,
        max_positions: Optional[int] = None
    ):
        """
        Initialize risk manager.
        
        Args:
            limits: Risk limits configuration
            config: Configuration manager
            logger: Logging manager
            max_position_pct: Override for max position %
            max_portfolio_risk_pct: Override for max portfolio risk %
            max_positions: Override for max positions
        """
        self.limits = limits or RiskLimits()
        self.config = config or ConfigManager()
        self.logger = logger or LoggingManager()
        
        # Apply overrides
        if max_position_pct is not None:
            self.limits.max_position_size_pct = max_position_pct
            self.max_position_pct = max_position_pct  # For backwards compatibility
        if max_portfolio_risk_pct is not None:
            self.limits.max_portfolio_risk_pct = max_portfolio_risk_pct  
            self.max_portfolio_risk_pct = max_portfolio_risk_pct  # For backwards compatibility
        if max_positions is not None:
            self.limits.max_positions = max_positions
        
        # Track daily statistics
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.positions: Dict[str, Position] = {}
    
    def can_open_position(
        self,
        signal: TradingSignal,
        portfolio_value: float,
        current_positions: Optional[List[Position]] = None
    ) -> tuple[bool, str]:
        """
        Check if a new position can be opened.
        
        Args:
            signal: Trading signal
            portfolio_value: Current portfolio value
            current_positions: List of current positions
            
        Returns:
            Tuple of (can_open, reason)
        """
        if current_positions is None:
            current_positions = list(self.positions.values())
        
        # Check position count limit
        if len(current_positions) >= self.limits.max_positions:
            return False, f"Maximum positions reached ({self.limits.max_positions})"
        
        # Check daily loss limit
        daily_loss_pct = abs(self.daily_pnl / portfolio_value * 100) if portfolio_value > 0 else 0
        if self.daily_pnl < 0 and daily_loss_pct >= self.limits.max_daily_loss_pct:
            return False, f"Daily loss limit reached ({daily_loss_pct:.1f}%)"
        
        # Check portfolio heat (total risk)
        current_heat = self._calculate_portfolio_heat(current_positions, portfolio_value)
        
        if signal.stop_loss:
            # Calculate risk for new position
            position_value = portfolio_value * (signal.position_size_pct / 100)
            shares = int(position_value / signal.price)
            risk_per_share = signal.price - signal.stop_loss
            position_risk = shares * risk_per_share
            position_risk_pct = (position_risk / portfolio_value) * 100
            
            new_heat = current_heat + position_risk_pct
            
            if new_heat > self.limits.max_portfolio_risk_pct:
                return False, f"Portfolio heat too high ({new_heat:.1f}%)"
        
        # Check position size limit
        position_size_pct = signal.position_size_pct
        if position_size_pct > self.limits.max_position_size_pct:
            return False, f"Position size too large ({position_size_pct:.1f}%)"
        
        # All checks passed
        return True, "Risk checks passed"
    
    def calculate_position_size(
        self,
        signal: TradingSignal,
        portfolio_value: float,
        risk_per_trade_pct: float = 1.0
    ) -> int:
        """
        Calculate optimal position size based on risk.
        
        Args:
            signal: Trading signal
            portfolio_value: Current portfolio value
            risk_per_trade_pct: Risk per trade as % of portfolio
            
        Returns:
            Number of shares to trade
        """
        if signal.stop_loss is None:
            self.logger.warning(f"No stop loss for {signal.symbol}, cannot size position")
            return 0
        
        # Risk amount in dollars
        risk_amount = portfolio_value * (risk_per_trade_pct / 100)
        
        # Risk per share
        risk_per_share = abs(signal.price - signal.stop_loss)
        
        if risk_per_share <= 0:
            self.logger.warning(f"Invalid risk per share for {signal.symbol}")
            return 0
        
        # Calculate shares based on risk
        shares = int(risk_amount / risk_per_share)
        
        # Cap at position size percentage
        max_position_value = portfolio_value * (signal.position_size_pct / 100)
        max_shares = int(max_position_value / signal.price)
        
        # Take the smaller of the two
        final_shares = min(shares, max_shares)
        
        # Ensure minimum position value of $500
        min_shares = max(1, int(500 / signal.price))
        
        return max(final_shares, min_shares) if final_shares > 0 else 0
    
    def _calculate_portfolio_heat(
        self,
        positions: List[Position],
        portfolio_value: float
    ) -> float:
        """
        Calculate total portfolio risk (heat).
        
        Args:
            positions: List of current positions
            portfolio_value: Current portfolio value
            
        Returns:
            Total risk as % of portfolio
        """
        total_risk = 0.0
        
        for pos in positions:
            risk_per_share = abs(pos.current_price - pos.stop_loss)
            position_risk = pos.quantity * risk_per_share
            total_risk += position_risk
        
        return (total_risk / portfolio_value * 100) if portfolio_value > 0 else 0.0
    
    def update_position(
        self,
        symbol: str,
        current_price: float
    ) -> bool:
        """
        Update position with current price.
        
        Args:
            symbol: Stock symbol
            current_price: Current market price
            
        Returns:
            True if stop loss or take profit hit
        """
        if symbol not in self.positions:
            return False
        
        pos = self.positions[symbol]
        pos.current_price = current_price
        
        # Calculate unrealized P&L
        pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
        pos.unrealized_pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
        
        # Check stop loss
        if current_price <= pos.stop_loss:
            self.logger.warning(
                f"Stop loss hit for {symbol}",
                extra={
                    'symbol': symbol,
                    'entry': pos.entry_price,
                    'current': current_price,
                    'stop': pos.stop_loss,
                    'pnl_pct': pos.unrealized_pnl_pct
                }
            )
            return True
        
        # Check take profit
        if pos.take_profit and current_price >= pos.take_profit:
            self.logger.info(
                f"Take profit hit for {symbol}",
                extra={
                    'symbol': symbol,
                    'entry': pos.entry_price,
                    'current': current_price,
                    'target': pos.take_profit,
                    'pnl_pct': pos.unrealized_pnl_pct
                }
            )
            return True
        
        return False
    
    def add_position(self, signal: TradingSignal, quantity: int):
        """Add a new position to tracking."""
        self.positions[signal.symbol] = Position(
            symbol=signal.symbol,
            quantity=quantity,
            entry_price=signal.price,
            current_price=signal.price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            unrealized_pnl=0.0,
            unrealized_pnl_pct=0.0
        )
        
        self.logger.info(
            f"Position added: {signal.symbol}",
            extra={
                'symbol': signal.symbol,
                'quantity': quantity,
                'entry_price': signal.price,
                'stop_loss': signal.stop_loss,
                'take_profit': signal.take_profit
            }
        )
    
    def remove_position(self, symbol: str, exit_price: float) -> float:
        """
        Remove position and record P&L.
        
        Args:
            symbol: Stock symbol
            exit_price: Exit price
            
        Returns:
            Realized P&L
        """
        if symbol not in self.positions:
            return 0.0
        
        pos = self.positions[symbol]
        realized_pnl = (exit_price - pos.entry_price) * pos.quantity
        realized_pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price) * 100
        
        self.daily_pnl += realized_pnl
        self.daily_trades += 1
        
        self.logger.log_trade(
            f"Position closed: {symbol}",
            extra={
                'symbol': symbol,
                'quantity': pos.quantity,
                'entry_price': pos.entry_price,
                'exit_price': exit_price,
                'pnl': realized_pnl,
                'pnl_pct': realized_pnl_pct
            }
        )
        
        del self.positions[symbol]
        return realized_pnl
    
    def get_portfolio_summary(self, portfolio_value: float) -> Dict:
        """Get portfolio risk summary."""
        positions = list(self.positions.values())
        
        total_unrealized = sum(pos.unrealized_pnl for pos in positions)
        portfolio_heat = self._calculate_portfolio_heat(positions, portfolio_value)
        
        return {
            'portfolio_value': portfolio_value,
            'num_positions': len(positions),
            'total_unrealized_pnl': total_unrealized,
            'portfolio_heat_pct': portfolio_heat,
            'daily_pnl': self.daily_pnl,
            'daily_trades': self.daily_trades,
            'positions': [
                {
                    'symbol': pos.symbol,
                    'quantity': pos.quantity,
                    'entry_price': pos.entry_price,
                    'current_price': pos.current_price,
                    'unrealized_pnl': pos.unrealized_pnl,
                    'unrealized_pnl_pct': pos.unrealized_pnl_pct
                }
                for pos in positions
            ]
        }
    
    def reset_daily_stats(self):
        """Reset daily statistics (call at market close)."""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.logger.info("Daily risk statistics reset")
    
    # ==================== Simplified API Methods ====================
    
    def can_take_more_risk(self, account_value: float, current_risk: float) -> bool:
        """
        Check if more risk can be taken based on portfolio limits.
        
        Args:
            account_value: Total account value
            current_risk: Current risk amount in dollars
            
        Returns:
            True if more risk can be taken
        """
        current_risk_pct = (current_risk / account_value * 100) if account_value > 0 else 0
        return current_risk_pct < self.limits.max_portfolio_risk_pct
    
    def can_open_position(self, current_positions: int) -> bool:
        """
        Check if a new position can be opened based on position count.
        
        Args:
            current_positions: Number of current positions
            
        Returns:
            True if new position can be opened
        """
        return current_positions < self.limits.max_positions
    
    def calculate_risk_amount(self, entry_price: float, stop_loss: float, shares: int) -> float:
        """
        Calculate risk amount for a position.
        
        Args:
            entry_price: Entry price per share
            stop_loss: Stop loss price
            shares: Number of shares
            
        Returns:
            Risk amount in dollars
        """
        risk_per_share = abs(entry_price - stop_loss)
        return risk_per_share * shares


# ==================== Convenience Functions ====================

def calculate_position_size(
    account_value: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_loss: float
) -> int:
    """
    Calculate position size based on risk.
    
    Args:
        account_value: Total account value
        risk_per_trade_pct: Risk per trade as % of account
        entry_price: Entry price per share
        stop_loss: Stop loss price
        
    Returns:
        Number of shares to trade
    """
    if entry_price <= 0 or stop_loss <= 0:
        return 0
    
    # Risk amount in dollars
    risk_amount = account_value * (risk_per_trade_pct / 100)
    
    # Risk per share
    risk_per_share = abs(entry_price - stop_loss)
    
    if risk_per_share <= 0:
        return 0
    
    # Calculate shares
    shares = int(risk_amount / risk_per_share)
    
    return shares
