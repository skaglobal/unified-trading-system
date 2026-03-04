"""
Unified Trading System - Configuration Manager
Consolidates best practices from ibkr-trader and trader.ai
"""
import os
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, validator
from pydantic_settings import BaseSettings
import yaml
from dotenv import load_dotenv


class IBKRConfig(BaseModel):
    """IBKR connection configuration"""
    host: str = "127.0.0.1"
    port: int = 4002  # Paper trading default
    client_id: int = 1
    readonly: bool = False
    account: Optional[str] = None
    timeout: int = 30


class TradingConfig(BaseModel):
    """Trading mode and safety settings"""
    mode: str = "paper"  # paper | live
    enable_auto_trading: bool = False
    max_position_size_pct: float = 2.0
    max_total_risk_pct: float = 10.0
    max_positions: int = 10
    max_daily_loss_pct: float = 3.0
    
    @validator('mode')
    def validate_mode(cls, v):
        if v not in ['paper', 'live']:
            raise ValueError("mode must be 'paper' or 'live'")
        return v


class DataSourcesConfig(BaseModel):
    """Data source configuration"""
    use_yahoo_finance: bool = True
    finviz_email: Optional[str] = None
    finviz_password: Optional[str] = None
    finviz_cookie: Optional[str] = None
    finviz_elite: bool = False


class AIConfig(BaseModel):
    """AI features configuration"""
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4-turbo-preview"
    enable_narratives: bool = False


class EmailConfig(BaseModel):
    """Email notification configuration"""
    enable_alerts: bool = False
    sendgrid_api_key: Optional[str] = None
    alert_from: Optional[str] = None
    alert_to: Optional[str] = None
    gmail_address: Optional[str] = None
    gmail_app_password: Optional[str] = None


class PathsConfig(BaseModel):
    """File paths configuration"""
    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    cache_dir: Path = Path("data/cache")
    
    def ensure_dirs(self):
        """Create directories if they don't exist"""
        for path_name in ['config_dir', 'data_dir', 'log_dir', 'cache_dir']:
            path = getattr(self, path_name)
            path.mkdir(parents=True, exist_ok=True)


class Config(BaseSettings):
    """Main configuration class combining all settings"""
    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    data_sources: DataSourcesConfig = Field(default_factory=DataSourcesConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    
    log_level: str = "INFO"
    log_to_file: bool = True
    log_to_console: bool = True
    debug: bool = False
    
    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"
        case_sensitive = False


class ConfigManager:
    """
    Configuration manager with support for:
    - Environment variables (.env)
    - YAML configuration files
    - Runtime overrides
    """
    
    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or Path("config")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        
        # Load environment variables
        load_dotenv()
        
        # Load base configuration
        self._config = Config()
        
        # Ensure directories exist
        self._config.paths.ensure_dirs()
        
        # Load YAML overrides if they exist
        self._load_yaml_configs()
    
    def _load_yaml_configs(self):
        """Load configuration from YAML files"""
        yaml_configs = [
            "trading_config.yaml",
            "strategies.yaml",
            "universe.yaml"
        ]
        
        self.yaml_data = {}
        for config_file in yaml_configs:
            config_path = self.config_dir / config_file
            if config_path.exists():
                with open(config_path, 'r') as f:
                    self.yaml_data[config_file] = yaml.safe_load(f)
    
    @property
    def config(self) -> Config:
        """Get current configuration"""
        return self._config
    
    def get_strategy_config(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        """Get configuration for a specific strategy"""
        strategies = self.yaml_data.get("strategies.yaml", {})
        return strategies.get("strategies", {}).get(strategy_name)
    
    def get_universe(self, universe_name: str = "default") -> list:
        """Get stock universe list"""
        universe_data = self.yaml_data.get("universe.yaml", {})
        return universe_data.get("universes", {}).get(universe_name, [])
    
    def is_paper_trading(self) -> bool:
        """Check if in paper trading mode"""
        return self._config.trading.mode == "paper"
    
    def is_auto_trading_enabled(self) -> bool:
        """Check if auto-trading is enabled"""
        return self._config.trading.enable_auto_trading
    
    def get_ibkr_params(self) -> dict:
        """Get IBKR connection parameters as dict"""
        return {
            "host": self._config.ibkr.host,
            "port": self._config.ibkr.port,
            "clientId": self._config.ibkr.client_id,
            "readonly": self._config.ibkr.readonly,
            "timeout": self._config.ibkr.timeout
        }
    
    def __repr__(self):
        return f"ConfigManager(mode={self._config.trading.mode}, auto_trading={self._config.trading.enable_auto_trading})"


# Global config instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """Get or create global config manager instance"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def reload_config():
    """Reload configuration (useful for testing)"""
    global _config_manager
    _config_manager = None
    return get_config_manager()
