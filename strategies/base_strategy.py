"""
Base Strategy - Abstract base class for all trading strategies.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

import pandas as pd

from core.config_manager import ConfigManager
from core.logging_manager import LoggingManager


class SignalType(Enum):
    """Trading signal types."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class SignalStrength(Enum):
    """Signal strength levels."""
    STRONG = 3
    MODERATE = 2
    WEAK = 1


@dataclass
class TradingSignal:
    """Trading signal with metadata."""
    symbol: str
    signal_type: SignalType
    strength: SignalStrength
    price: float
    timestamp: datetime
    indicators: Dict
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_size_pct: float = 2.0  # % of portfolio


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.
    
    Subclasses must implement:
    - generate_signals(): Main signal generation logic
    - validate_signal(): Signal validation rules
    """
    
    def __init__(
        self,
        name: str,
        config: Optional[ConfigManager] = None,
        logger: Optional[LoggingManager] = None
    ):
        """
        Initialize strategy.
        
        Args:
            name: Strategy name
            config: Configuration manager
            logger: Logging manager
        """
        self.name = name
        self.config = config or ConfigManager()
        self.logger = logger or LoggingManager()
        
        # Strategy parameters (override in subclasses)
        self.min_volume = 500000  # Minimum average volume
        self.min_price = 5.0  # Minimum price
        self.max_price = 500.0  # Maximum price
        self.max_positions = 10  # Maximum concurrent positions
        
        # Performance tracking
        self.signals_generated = 0
        self.signals_validated = 0
    
    @abstractmethod
    def generate_signals(self, data: Dict[str, pd.DataFrame]) -> List[TradingSignal]:
        """
        Generate trading signals from market data.
        
        Args:
            data: Dictionary mapping symbol to DataFrame with OHLCV + indicators
            
        Returns:
            List of trading signals
        """
        pass
    
    @abstractmethod
    def validate_signal(self, signal: TradingSignal, df: pd.DataFrame) -> bool:
        """
        Validate a trading signal.
        
        Args:
            signal: Trading signal to validate
            df: DataFrame with market data and indicators
            
        Returns:
            True if signal is valid
        """
        pass
    
    def filter_universe(self, data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """
        Filter universe based on basic criteria.
        
        Args:
            data: Dictionary mapping symbol to DataFrame
            
        Returns:
            Filtered dictionary
        """
        filtered = {}
        
        for symbol, df in data.items():
            if df is None or df.empty:
                continue
            
            # Check price range
            current_price = df['Close'].iloc[-1]
            if not (self.min_price <= current_price <= self.max_price):
                continue
            
            # Check volume
            if 'Volume' in df.columns:
                avg_volume = df['Volume'].tail(20).mean()
                if avg_volume < self.min_volume:
                    continue
            
            # Check data quality
            if len(df) < 50:  # Need minimum history
                continue
            
            filtered[symbol] = df
        
        return filtered
    
    def calculate_position_size(
        self,
        signal: TradingSignal,
        portfolio_value: float,
        risk_per_trade: float = 0.02
    ) -> int:
        """
        Calculate position size based on risk management.
        
        Args:
            signal: Trading signal
            portfolio_value: Total portfolio value
            risk_per_trade: Risk per trade as fraction of portfolio
            
        Returns:
            Number of shares to trade
        """
        if signal.stop_loss is None:
            return 0
        
        # Risk amount in dollars
        risk_amount = portfolio_value * risk_per_trade
        
        # Risk per share
        risk_per_share = abs(signal.price - signal.stop_loss)
        
        if risk_per_share == 0:
            return 0
        
        # Calculate shares
        shares = int(risk_amount / risk_per_share)
        
        # Cap at position size percentage
        max_position_value = portfolio_value * (signal.position_size_pct / 100)
        max_shares = int(max_position_value / signal.price)
        
        return min(shares, max_shares)
    
    def get_metrics(self) -> Dict:
        """Get strategy performance metrics."""
        return {
            'name': self.name,
            'signals_generated': self.signals_generated,
            'signals_validated': self.signals_validated,
            'validation_rate': (
                self.signals_validated / self.signals_generated 
                if self.signals_generated > 0 else 0
            )
        }
