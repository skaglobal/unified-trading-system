"""
Unified Trading System - Logging Manager
Consolidates logging patterns from ibkr-trader and trader.ai
"""
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from pythonjsonlogger import jsonlogger


class ColoredFormatter(logging.Formatter):
    """Colored console output formatter"""
    
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record):
        if record.levelname in self.COLORS:
            record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
        return super().format(record)


class LoggingManager:
    """
    Centralized logging manager with support for:
    - Console output (colored)
    - File output (JSON structured logs)
    - Multiple loggers for different components
    """
    
    def __init__(
        self,
        log_dir: Path = Path("logs"),
        log_level: str = "INFO",
        log_to_console: bool = True,
        log_to_file: bool = True,
        app_name: str = "unified_trading"
    ):
        self.log_dir = log_dir
        self.log_level = getattr(logging, log_level.upper())
        self.log_to_console = log_to_console
        self.log_to_file = log_to_file
        self.app_name = app_name
        
        # Ensure log directory exists
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Configure root logger
        self._setup_root_logger()
        
        # Store created loggers
        self._loggers = {}
    
    def _setup_root_logger(self):
        """Setup root logger with handlers"""
        root_logger = logging.getLogger()
        root_logger.setLevel(self.log_level)
        
        # Clear existing handlers
        root_logger.handlers.clear()
        
        # Console handler (colored)
        if self.log_to_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(self.log_level)
            console_format = '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'
            console_formatter = ColoredFormatter(console_format, datefmt='%H:%M:%S')
            console_handler.setFormatter(console_formatter)
            root_logger.addHandler(console_handler)
        
        # File handler (JSON)
        if self.log_to_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = self.log_dir / f"{self.app_name}_{timestamp}.log"
            
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(self.log_level)
            
            # JSON formatter for structured logs
            json_format = '%(asctime)s %(name)s %(levelname)s %(message)s'
            json_formatter = jsonlogger.JsonFormatter(json_format)
            file_handler.setFormatter(json_formatter)
            root_logger.addHandler(file_handler)
            
            # Also create a human-readable log
            readable_log = self.log_dir / f"{self.app_name}_{timestamp}_readable.log"
            readable_handler = logging.FileHandler(readable_log)
            readable_handler.setLevel(self.log_level)
            readable_format = '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'
            readable_formatter = logging.Formatter(readable_format, datefmt='%Y-%m-%d %H:%M:%S')
            readable_handler.setFormatter(readable_formatter)
            root_logger.addHandler(readable_handler)
    
    def get_logger(self, name: str) -> logging.Logger:
        """Get or create a logger for a component"""
        if name not in self._loggers:
            logger = logging.getLogger(name)
            self._loggers[name] = logger
        return self._loggers[name]

    # ---- Proxy methods so LoggingManager can be used directly as a logger ----
    def _default_logger(self) -> logging.Logger:
        return self.get_logger(self.app_name)

    def debug(self, msg: str, *args, **kwargs):
        self._default_logger().debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        self._default_logger().info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        self._default_logger().warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        self._default_logger().error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        self._default_logger().critical(msg, *args, **kwargs)
    
    def log_trade(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        reason: str,
        **kwargs
    ):
        """Log a trade with structured data"""
        trade_logger = self.get_logger("trading.trades")
        trade_logger.info(
            f"TRADE: {action} {quantity} {symbol} @ ${price:.2f}",
            extra={
                "event_type": "trade",
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "price": price,
                "reason": reason,
                **kwargs
            }
        )
    
    def log_signal(
        self,
        symbol: str,
        signal_type: str,
        strength: float,
        **kwargs
    ):
        """Log a trading signal"""
        signal_logger = self.get_logger("trading.signals")
        signal_logger.info(
            f"SIGNAL: {signal_type} on {symbol} (strength: {strength:.2f})",
            extra={
                "event_type": "signal",
                "symbol": symbol,
                "signal_type": signal_type,
                "strength": strength,
                **kwargs
            }
        )
    
    def log_error(
        self,
        component: str,
        error: Exception,
        context: Optional[dict] = None
    ):
        """Log an error with context"""
        error_logger = self.get_logger(f"errors.{component}")
        error_logger.error(
            f"ERROR in {component}: {str(error)}",
            extra={
                "event_type": "error",
                "component": component,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "context": context or {}
            },
            exc_info=True
        )
    
    def log_performance(
        self,
        strategy: str,
        total_pnl: float,
        win_rate: float,
        sharpe_ratio: float,
        **kwargs
    ):
        """Log performance metrics"""
        perf_logger = self.get_logger("trading.performance")
        perf_logger.info(
            f"PERFORMANCE: {strategy} | PnL: ${total_pnl:.2f} | Win Rate: {win_rate:.1%} | Sharpe: {sharpe_ratio:.2f}",
            extra={
                "event_type": "performance",
                "strategy": strategy,
                "total_pnl": total_pnl,
                "win_rate": win_rate,
                "sharpe_ratio": sharpe_ratio,
                **kwargs
            }
        )


# Global logging manager instance
_logging_manager: Optional[LoggingManager] = None


def get_logging_manager(
    log_dir: Path = Path("logs"),
    log_level: str = "INFO",
    log_to_console: bool = True,
    log_to_file: bool = True
) -> LoggingManager:
    """Get or create global logging manager instance"""
    global _logging_manager
    if _logging_manager is None:
        _logging_manager = LoggingManager(
            log_dir=log_dir,
            log_level=log_level,
            log_to_console=log_to_console,
            log_to_file=log_to_file
        )
    return _logging_manager


def get_logger(name: str) -> logging.Logger:
    """Convenience function to get a logger"""
    return get_logging_manager().get_logger(name)
