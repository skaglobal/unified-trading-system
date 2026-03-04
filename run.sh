#!/bin/bash
#
# Unified Trading System - Main Entry Point
# Launches the Streamlit dashboard
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo -e "${RED}Error: Virtual environment not found${NC}"
    echo -e "${YELLOW}Run ./setup.sh first${NC}"
    exit 1
fi

# Activate virtual environment
source venv/bin/activate

# Check if .env exists
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}Warning: .env file not found${NC}"
    echo -e "${YELLOW}Using default configuration${NC}"
fi

# Set PYTHONPATH
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

# Get port from .env or use default
PORT=${STREAMLIT_PORT:-8080}

# Launch Streamlit
echo -e "${BLUE}Starting Unified Trading System Dashboard...${NC}"
echo -e "${GREEN}Dashboard will be available at: http://localhost:$PORT${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
echo ""

streamlit run streamlit_app.py --server.port $PORT --server.address localhost
