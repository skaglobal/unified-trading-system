"""
Unified Trading System - Utility Functions
Common utilities used across the system
"""
from datetime import datetime, time, timezone
from typing import Optional
import pytz


def is_market_open(
    dt: Optional[datetime] = None,
    tz: str = "America/New_York"
) -> bool:
    """
    Check if US stock market is open
    
    Args:
        dt: DateTime to check (defaults to now)
        tz: Timezone string (defaults to US Eastern)
    
    Returns:
        True if market is open, False otherwise
    """
    if dt is None:
        dt = datetime.now(pytz.timezone(tz))
    elif dt.tzinfo is None:
        dt = pytz.timezone(tz).localize(dt)
    else:
        dt = dt.astimezone(pytz.timezone(tz))
    
    # Check if weekend
    if dt.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    # Market hours: 9:30 AM - 4:00 PM ET
    market_open = time(9, 30)
    market_close = time(16, 0)
    current_time = dt.time()
    
    return market_open <= current_time <= market_close


def format_currency(amount: float, decimals: int = 2) -> str:
    """Format amount as currency"""
    return f"${amount:,.{decimals}f}"


def format_percentage(value: float, decimals: int = 2) -> str:
    """Format value as percentage"""
    return f"{value * 100:.{decimals}f}%"


def calculate_position_size(
    account_value: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float
) -> int:
    """
    Calculate position size based on risk
    
    Args:
        account_value: Total account value
        risk_pct: Risk percentage (e.g., 0.01 for 1%)
        entry_price: Entry price per share
        stop_price: Stop loss price per share
    
    Returns:
        Number of shares to buy
    """
    if entry_price <= 0 or stop_price <= 0:
        return 0
    
    if entry_price <= stop_price:
        return 0  # Invalid stop
    
    risk_amount = account_value * risk_pct
    risk_per_share = entry_price - stop_price
    
    if risk_per_share <= 0:
        return 0
    
    shares = int(risk_amount / risk_per_share)
    return max(0, shares)


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safely divide, returning default if denominator is zero"""
    if denominator == 0:
        return default
    return numerator / denominator


def round_to_tick(price: float, tick_size: float = 0.01) -> float:
    """Round price to nearest tick size"""
    return round(price / tick_size) * tick_size


def validate_symbol(symbol: str) -> bool:
    """Basic symbol validation"""
    if not symbol:
        return False
    
    # Must be uppercase letters only, 1-5 characters
    if not symbol.isalpha():
        return False
    
    if len(symbol) < 1 or len(symbol) > 5:
        return False
    
    return True


class RateLimiter:
    """Simple rate limiter for API calls"""
    
    def __init__(self, max_calls: int, time_window: float):
        """
        Args:
            max_calls: Maximum calls allowed
            time_window: Time window in seconds
        """
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = []
    
    def is_allowed(self) -> bool:
        """Check if a call is allowed"""
        now = datetime.now().timestamp()
        
        # Remove old calls outside time window
        self.calls = [call_time for call_time in self.calls 
                     if now - call_time < self.time_window]
        
        # Check if under limit
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        
        return False
    
    def wait_time(self) -> float:
        """Get seconds to wait before next call is allowed"""
        if len(self.calls) < self.max_calls:
            return 0.0
        
        now = datetime.now().timestamp()
        oldest_call = self.calls[0]
        return max(0.0, self.time_window - (now - oldest_call))
