# Unified Trading System

A consolidated, production-ready Python trading system that combines the best features from multiple trading repositories into a single, maintainable codebase.

## 🎯 Overview

This system consolidates 8+ separate Python trading implementations into one unified platform with:
- ✅ **Single Streamlit UI** for all functionality
- ✅ **Modular architecture** preserving best features
- ✅ **Multiple strategies**: Swing, Intraday, Scalping, Options
- ✅ **Paper trading first** with live trading support
- ✅ **Comprehensive risk management**
- ✅ **IBKR integration** via ib_insync
- ✅ **Multiple data sources**: Yahoo Finance, Finviz

## 🚀 Quick Start

### Prerequisites
- Python 3.11+ 
- Interactive Brokers TWS or Gateway
- (Optional) Finviz Elite subscription
- (Optional) OpenAI API key for AI narratives

### Installation

```bash
# 1. Clone or navigate to repository
cd /Users/senthil/skaglobal.dev/unified-trading-system

# 2. Run setup (creates venv, installs dependencies, creates configs)
./setup.sh

# 3. Edit environment configuration
nano .env  # Configure IBKR, API keys, etc.

# 4. Start IBKR (Paper Trading: port 4002)
# Launch TWS or Gateway

# 5. Launch dashboard
./run.sh
```

Visit http://localhost:8080 to access the dashboard.

## 📁 Project Structure

```
unified-trading-system/
├── streamlit_app.py          # Main Streamlit dashboard entry point
├── setup.sh                  # One-time setup script
├── run.sh                    # Launch dashboard
├── requirements.txt          # All dependencies
├── .env.example             # Environment template
│
├── core/                    # Core framework
│   ├── config_manager.py   # Configuration handling
│   ├── logging_manager.py  # Centralized logging
│   └── utils.py            # Common utilities
│
├── connectors/              # External integrations
│   ├── ibkr_connector.py   # IBKR wrapper (ib_insync)
│   ├── finviz_scraper.py   # Finviz data fetcher
│   └── yahoo_finance.py    # yfinance wrapper
│
├── data/                    # Data management
│   ├── market_data.py      # Data fetching & caching
│   ├── universe_manager.py # Stock universe handling
│   └── storage.py          # Persistence (SQLite)
│
├── analysis/                # Analysis engines
│   ├── technical_indicators.py  # ATR, EMA, RSI, etc.
│   ├── market_regime.py        # Regime detection
│   ├── iei_scorer.py           # IEI scoring
│   └── pattern_detection.py   # Chart patterns
│
├── strategies/              # Trading strategies
│   ├── base_strategy.py    # Abstract base
│   ├── swing_trading.py    # Swing strategy
│   ├── intraday_momentum.py # Intraday strategy
│   ├── scalping.py         # Scalping strategy
│   └── options_strategy.py # Options strategy
│
├── risk/                    # Risk management
│   ├── position_sizer.py   # Position sizing
│   ├── portfolio_manager.py # Position tracking
│   ├── drawdown_guard.py   # Drawdown protection
│   └── constraints.py      # Portfolio limits
│
├── execution/               # Order management
│   ├── order_manager.py    # Order handling
│   ├── paper_trading.py    # Simulated execution
│   └── live_trading.py     # Real execution
│
├── monitoring/              # Real-time monitoring
│   ├── signal_monitor.py   # Signal detection
│   ├── performance_tracker.py # P&L tracking
│   └── alert_manager.py    # Notifications
│
├── backtesting/             # Strategy testing
│   ├── backtest_engine.py  # Backtester
│   └── reports.py          # Performance analytics
│
├── narration/               # AI insights
│   └── trade_narrator.py   # LLM trade narratives
│
├── scripts/                 # Utility scripts
│   ├── scan_daily.py       # Daily scanner
│   ├── update_universe.py  # Universe refresh
│   └── backtest_strategy.py # Backtest runner
│
├── config/                  # Configuration files
│   ├── trading_config.yaml # Risk parameters
│   ├── strategies.yaml     # Strategy configs
│   └── universe.yaml       # Stock lists
│
├── tests/                   # Unit tests
└── logs/                    # Log files
```

## 🎨 Dashboard Features

The Streamlit dashboard provides 8 main pages:

1. **Market Overview** - Market regime, sector performance, top movers
2. **Stock Scanner** - Multiple scanners (Swing, Intraday, Pre-market, IEI)
3. **Live Monitoring** - Real-time watchlist, positions, signals, P&L
4. **Strategy Manager** - Configure and enable/disable strategies
5. **Portfolio** - Current positions, trade history, performance
6. **Backtesting** - Test strategies on historical data
7. **AI Insights** - LLM trade narratives and pattern recognition
8. **Configuration** - System settings, IBKR connection, API keys

## 📊 Supported Strategies

### Swing Trading (3-10 day holds)
- Regime-filtered entries (SPY > MA200)
- Pullback to MA20/MA50
- ATR-based position sizing
- No hard stops, signal-based exits

### Intraday Momentum (30min - 4hr holds)
- Opening range breakouts
- Volume surge detection
- VWAP positioning
- Trailing stops

### Scalping (1-15 min holds) 
- High-frequency trades
- Tight profit targets (0.5%)
- Strict liquidity requirements
- Max 5 trades per day

### Options (Intraday/Weekly)
- High-probability setups
- Delta-neutral strategies
- IV rank filtering
- Automated Greeks monitoring

## 🛡️ Risk Management

- **Position Sizing**: ATR-based, account % risk
- **Portfolio Limits**: Max positions, sector exposure
- **Drawdown Protection**: 3-tier (10% pause, 15% reduce, 20% stop)
- **Daily Loss Circuit Breaker**: 3% maximum
- **Paper Trading First**: Always test before live

## 🔧 Configuration

### .env File
```bash
# IBKR Connection
IBKR_HOST=127.0.0.1
IBKR_PORT=4002                    # Paper: 4002, Live: 7496
IBKR_CLIENT_ID=1

# Trading Mode
TRADING_MODE=paper                # paper | live
ENABLE_AUTO_TRADING=false

# Data Sources
USE_YAHOO_FINANCE=true
FINVIZ_EMAIL=your-email@example.com
FINVIZ_PASSWORD=your-password

# Optional: AI Features
OPENAI_API_KEY=sk-...

# Optional: Email Alerts
GMAIL_ADDRESS=your-email@gmail.com
GMAIL_APP_PASSWORD=your-app-password
```

### Strategy Configuration (config/strategies.yaml)
```yaml
strategies:
  swing_trading:
    enabled: true
    timeframe: "daily"
    holding_period_days: "3-10"
    # ... strategy parameters
  
  intraday_momentum:
    enabled: true
    timeframe: "5min"
    # ... strategy parameters
```

### Universe Configuration (config/universe.yaml)
```yaml
universes:
  default:
    - AAPL
    - MSFT
    - GOOGL
    # ... more symbols
```

## 📜 Shell Scripts

```bash
./setup.sh          # Initial setup (run once)
./run.sh            # Launch dashboard
./scan.sh           # Run daily scanner
./backtest.sh       # Run backtests
./update.sh         # Update data & universe
```

## 🧪 Testing

```bash
# Activate venv
source venv/bin/activate

# Run all tests
pytest

# Run specific test suite
pytest tests/test_connectors/
pytest tests/test_strategies/

# Run with coverage
pytest --cov=. --cov-report=html
```

## 📝 Logging

Logs are written to `logs/` directory:
- `unified_trading_TIMESTAMP.log` - JSON structured logs
- `unified_trading_TIMESTAMP_readable.log` - Human-readable logs

Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

## 🔒 Safety Features

- ✅ Paper trading default mode
- ✅ Manual approval required for live trading
- ✅ Kill switch file support
- ✅ Position reconciliation with IBKR
- ✅ Connection health checks
- ✅ Automatic reconnection with backoff
- ✅ 3-tier drawdown protection
- ✅ Daily loss circuit breaker

## 🎓 Learning Resources

- **Architecture**: See inline documentation in each module
- **Strategies**: Review `strategies/` for implementation details
- **Risk Management**: Check `risk/` modules for position sizing
- **Backtesting**: Use `backtesting/` to validate strategies

## 🤝 Contributing

This is a consolidated system from multiple repositories. When adding features:

1. Follow existing module structure
2. Add tests for new functionality
3. Update configuration files as needed
4. Document changes in code comments

## 📄 License

Private repository. All rights reserved.

## 🐛 Troubleshooting

**Cannot connect to IBKR**
- Ensure TWS/Gateway is running
- Check port in .env matches IBKR settings
- Enable "Socket Clients" in IBKR API settings

**Module not found errors**
- Run `./setup.sh` to reinstall dependencies
- Ensure virtual environment is activated

**Streamlit errors**
- Check logs in `logs/` directory
- Verify .env configuration
- Try restarting dashboard

## 📞 Support

For issues or questions, check:
- Inline code documentation
- Configuration examples in `config/`
- Log files in `logs/`

---

**Version**: 1.0.0  
**Phase**: Phase 1 - Foundation Complete  
**Date**: March 4, 2026  
**Status**: Ready for Phase 2 (Connectors)
