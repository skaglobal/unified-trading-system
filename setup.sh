#!/bin/bash
#
# Unified Trading System - Setup Script
# One-time setup for the trading system
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${BLUE}║   Unified Trading System - Setup              ║${NC}"
echo -e "${BOLD}${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo ""

# Check Python version
echo -e "${BLUE}Checking Python version...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 not found${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}✓${NC} Found Python $PYTHON_VERSION"

# Check if Python >= 3.11
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo -e "${YELLOW}Warning: Python 3.11+ recommended, you have $PYTHON_VERSION${NC}"
fi

# Create virtual environment
echo ""
echo -e "${BLUE}Creating virtual environment...${NC}"
if [ -d "venv" ]; then
    echo -e "${YELLOW}Virtual environment already exists. Removing...${NC}"
    rm -rf venv
fi

python3 -m venv venv
echo -e "${GREEN}✓${NC} Virtual environment created"

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
echo ""
echo -e "${BLUE}Upgrading pip...${NC}"
pip install --upgrade pip > /dev/null 2>&1
echo -e "${GREEN}✓${NC} pip upgraded"

# Install dependencies
echo ""
echo -e "${BLUE}Installing dependencies... (this may take a few minutes)${NC}"
pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} Dependencies installed successfully"
else
    echo -e "${RED}Error installing dependencies${NC}"
    exit 1
fi

# Create .env file if it doesn't exist
echo ""
echo -e "${BLUE}Setting up environment configuration...${NC}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "${GREEN}✓${NC} Created .env file from template"
    echo -e "${YELLOW}⚠${NC}  Please edit .env with your settings"
else
    echo -e "${YELLOW}⚠${NC}  .env file already exists (not overwriting)"
fi

# Create necessary directories
echo ""
echo -e "${BLUE}Creating directory structure...${NC}"
mkdir -p logs data/cache data/historical config

# Create sample config files
if [ ! -f "config/trading_config.yaml" ]; then
    cat > config/trading_config.yaml << 'EOF'
# Trading Configuration
# Adjust these parameters based on your strategy

# Risk Management
risk:
  max_position_size_pct: 2.0      # % of account per position
  max_total_risk_pct: 10.0        # % of account total risk
  max_positions: 10               # Maximum concurrent positions
  max_daily_loss_pct: 3.0         # Daily circuit breaker
  
  # Drawdown protection (3-tier)
  pause_trading_at_pct: 10.0      # Pause at 10% drawdown
  reduce_size_at_pct: 15.0        # Reduce position size at 15%
  stop_trading_at_pct: 20.0       # Full stop at 20%

# Position Sizing
position_sizing:
  method: "atr"                   # atr | fixed | kelly
  atr_multiplier: 2.0             # Stop distance in ATR
  min_shares: 1
  max_shares: 1000

# Market Hours
market:
  timezone: "America/New_York"
  pre_market_start: "04:00"
  market_open: "09:30"
  market_close: "16:00"
  after_hours_end: "20:00"
EOF
    echo -e "${GREEN}✓${NC} Created config/trading_config.yaml"
fi

if [ ! -f "config/strategies.yaml" ]; then
    cat > config/strategies.yaml << 'EOF'
# Strategy Configurations

strategies:
  swing_trading:
    enabled: true
    timeframe: "daily"
    holding_period_days: "3-10"
    entry_criteria:
      - regime_filter: "risk_on"
      - trend: "price > ma200"
      - pullback: "near_ma20_or_ma50"
      - confirmation: "volume_breakout"
    exit_criteria:
      - trend_invalidation: "close < ma200"
      - regime_flip: "risk_off"
      - time_stop_days: 15
  
  intraday_momentum:
    enabled: true
    timeframe: "5min"
    holding_period_minutes: "30-240"
    entry_criteria:
      - opening_range_breakout: true
      - volume_surge: "> 2x"
      - vwap_position: "above"
    exit_criteria:
      - vwap_cross: "below"
      - time_stop_minutes: 240
      - trailing_stop_pct: 2.0
  
  scalping:
    enabled: false
    timeframe: "1min"
    holding_period_minutes: "1-15"
    max_trades_per_day: 5
    entry_criteria:
      - momentum: "strong"
      - spread: "< 0.5%"
      - liquidity: "high"
    exit_criteria:
      - profit_target_pct: 0.5
      - stop_loss_pct: 0.3
      - time_stop_minutes: 15
EOF
    echo -e "${GREEN}✓${NC} Created config/strategies.yaml"
fi

if [ ! -f "config/universe.yaml" ]; then
    cat > config/universe.yaml << 'EOF'
# Stock Universe Configuration

universes:
  default:
    # Large cap tech stocks
    - AAPL
    - MSFT
    - GOOGL
    - AMZN
    - META
    - NVDA
    - TSLA
    
  sp500_liquid:
    # Highly liquid S&P 500 stocks
    - SPY   # S&P 500 ETF
    - QQQ   # NASDAQ 100 ETF
    - AAPL
    - MSFT
    - GOOGL
    - AMZN
    - NVDA
    - TSLA
    - META
    - JPM
    - V
    - WMT
    - DIS
    - NFLX
    - AMD
    - INTC
  
  swing_candidates:
    # Good swing trading candidates
    - AAPL
    - MSFT
    - AMD
    - NVDA
    - TSLA
    - BA
    - F
    - GE
    
  etfs:
    # Major ETFs
    - SPY   # S&P 500
    - QQQ   # NASDAQ 100
    - IWM   # Russell 2000
    - DIA   # Dow Jones
    - XLF   # Financials
    - XLE   # Energy
    - XLK   # Technology
EOF
    echo -e "${GREEN}✓${NC} Created config/universe.yaml"
fi

echo -e "${GREEN}✓${NC} Directory structure created"

# Git initialization (optional)
if [ ! -d ".git" ]; then
    echo ""
    echo -e "${BLUE}Initialize git repository? [y/N]${NC}"
    read -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        git init
        echo -e "${GREEN}✓${NC} Git repository initialized"
    fi
fi

# Summary
echo ""
echo -e "${BOLD}${GREEN}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   Setup Complete! 🚀                           ║${NC}"
echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Next Steps:${NC}"
echo ""
echo -e "1. ${BOLD}Configure environment:${NC}"
echo -e "   ${BLUE}nano .env${NC}  (or use your favorite editor)"
echo ""
echo -e "2. ${BOLD}Start IBKR Trader Workstation or Gateway${NC}"
echo -e "   Paper Trading: port 4002"
echo -e "   Live Trading:  port 7496 (TWS) or 4001 (Gateway)"
echo ""
echo -e "3. ${BOLD}Launch the dashboard:${NC}"
echo -e "   ${BLUE}./run.sh${NC}"
echo ""
echo -e "4. ${BOLD}Or run individual scripts:${NC}"
echo -e "   ${BLUE}./scan.sh${NC}       - Run daily scanner"
echo -e "   ${BLUE}./backtest.sh${NC}   - Run backtests"
echo ""
echo -e "${YELLOW}⚠${NC}  Remember: Always start with PAPER TRADING!"
echo ""
