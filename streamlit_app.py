"""
Unified Trading System - Streamlit Dashboard
Main entry point for the web interface
"""
import streamlit as st
from pathlib import Path
import sys

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.config_manager import get_config_manager
from core.logging_manager import get_logging_manager, get_logger

# Page config
st.set_page_config(
    page_title="Unified Trading System",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize managers
config = get_config_manager()
logging_mgr = get_logging_manager(
    log_level=config.config.log_level,
    log_to_console=config.config.log_to_console,
    log_to_file=config.config.log_to_file
)
logger = get_logger("dashboard.main")

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 3rem;
        font-weight: bold;
        text-align: center;
        color: #1f77b4;
        padding: 1rem;
    }
    .status-box {
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    .status-paper {
        background-color: #d4edda;
        border: 2px solid #28a745;
    }
    .status-live {
        background-color: #f8d7da;
        border: 2px solid #dc3545;
    }
</style>
""", unsafe_allow_html=True)

# Sidebar
with st.sidebar:
    st.title("🎯 Navigation")
    
    # Trading mode indicator
    mode = config.config.trading.mode
    mode_color = "🟢" if mode == "paper" else "🔴"
    st.markdown(f"### {mode_color} Mode: **{mode.upper()}**")
    
    if not config.is_paper_trading():
        st.warning("⚠️ LIVE TRADING MODE - BE CAREFUL!")
    
    # Navigation
    page = st.radio(
        "Select Page",
        [
            "🏠 Home",
            "📊 Market Overview",  
            "🔍 Stock Scanner",
            "📈 Live Monitoring",
            "⚙️ Strategy Manager",
            "💼 Portfolio",
            "📉 Backtesting",
            "🤖 AI Insights",
            "⚙️ Configuration"
        ],
        index=0
    )
    
    st.markdown("---")
    
    # Quick stats placeholder
    st.subheader("Quick Stats")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Positions", "0")
    with col2:
        st.metric("Daily P&L", "$0.00")
    
    st.markdown("---")
    st.caption("Unified Trading System v1.0")
    st.caption("Phase 1: Foundation")

# Import page modules
from pages import market_overview, scanner, backtesting, portfolio

# Main content area
if "🏠 Home" in page:
    st.markdown('<h1 class="main-header">📈 Unified Trading System</h1>', unsafe_allow_html=True)
    
    # Status box
    if config.is_paper_trading():
        st.markdown('''
        <div class="status-box status-paper">
            <h3>✅ Paper Trading Mode</h3>
            <p>Safe simulation environment - No real money at risk</p>
        </div>
        ''', unsafe_allow_html=True)
    else:
        st.markdown('''
        <div class="status-box status-live">
            <h3>⚠️ LIVE Trading Mode</h3>
            <p>REAL MONEY - Trades will be executed with real funds!</p>
        </div>
        ''', unsafe_allow_html=True)
    
    # Welcome message
    st.markdown("""
    ## Welcome! 🚀
    
    This is the **Unified Trading System** - a consolidated platform combining the best features 
    from multiple trading systems into one powerful solution.
    
    ### 🎯 Quick Start
    
    1. **Configure** - Check Configuration page to set up IBKR connection
    2. **Scan** - Use Stock Scanner to find trading opportunities  
    3. **Monitor** - Watch Live Monitoring for real-time signals
    4. **Trade** - Execute through Strategy Manager (start with Paper mode!)
    
    ### 📋 Available Features
    """)
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown("""
        **📊 Analysis**
        - Market regime detection
        - Technical indicators
        - IEI scoring
        - Pattern recognition
        """)
    
    with col2:
        st.markdown("""
        **📈 Strategies**
        - Swing trading
        - Intraday momentum
        - Scalping
        - Options trading
        """)
    
    with col3:
        st.markdown("""
        **🛡️ Risk Management**
        - ATR position sizing
        - Drawdown protection
        - Portfolio limits
        - Daily loss circuit breaker
        """)
    
    st.markdown("---")
    
    # System status
    st.subheader("🔧 System Status")
    
    status_col1, status_col2, status_col3 = st.columns(3)
    
    with status_col1:
        st.info("**Configuration**\n✅ Loaded")
    
    with status_col2:
        # Check IBKR connection (placeholder)
        st.warning("**IBKR Connection**\n⚠️ Not connected")
    
    with status_col3:
        st.info("**Data Sources**\n✅ Yahoo Finance ready")
    
    st.markdown("---")
    
    # Recent activity placeholder
    st.subheader("📝 Recent Activity")
    st.info("No recent activity. System ready for trading.")
    
    logger.info("Dashboard home page loaded")

elif "📊 Market Overview" in page:
    market_overview.render()

elif "🔍 Stock Scanner" in page:
    scanner.render()

elif "📈 Live Monitoring" in page:
    st.title("📈 Live Monitoring")
    st.info("**Coming Soon**: Real-time signal monitoring and position tracking")
    st.markdown("""
    This page will show:
    - Active watchlist
    - Current positions
    - Live signals
    - Real-time P&L
    - Active alerts
    """)

elif "⚙️ Strategy Manager" in page:
    st.title("⚙️ Strategy Manager")
    st.info("**Coming Soon**: Enable/disable strategies and configure parameters")
    
    st.subheader("Available Strategies")
    
    strategies = ["Swing Trading", "Intraday Momentum", "Scalping", "Options Trading"]
    for strategy in strategies:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(f"**{strategy}**")
        with col2:
            st.checkbox("Enable", key=f"enable_{strategy}", disabled=True)

elif "💼 Portfolio" in page:
    portfolio.render()

elif "📉 Backtesting" in page:
    backtesting.render()

elif "🤖 AI Insights" in page:
    st.title("🤖 AI Insights")
    st.info("**Coming in Phase 5**: LLM-powered trade narratives and insights")
    st.markdown("""
    This page will show:
    - AI-generated trade explanations
    - Pattern recognition insights
    - Risk warnings
    - Market commentary
    - Strategy suggestions
    """)

elif "⚙️ Configuration" in page:
    st.title("⚙️ Configuration")
    
    tab1, tab2, tab3 = st.tabs(["IBKR Connection", "Risk Settings", "Data Sources"])
    
    with tab1:
        st.subheader("IBKR Connection")
        
        current_config = config.get_ibkr_params()
        
        st.text_input("Host", value=current_config['host'], disabled=True)
        st.number_input("Port", value=current_config['port'], disabled=True)
        st.number_input("Client ID", value=current_config['clientId'], disabled=True)
        
        st.info("To change settings, edit the .env file and restart the dashboard")
        
        if st.button("Test Connection"):
            st.warning("Connection testing will be implemented in Phase 2")
    
    with tab2:
        st.subheader("Risk Management Settings")
        
        st.slider("Max Position Size %", 0.0, 10.0, 2.0, 0.5, disabled=True)
        st.slider("Max Total Risk %", 0.0, 20.0, 10.0, 1.0, disabled=True)
        st.number_input("Max Positions", 1, 50, 10, disabled=True)
        st.slider("Daily Loss Circuit Breaker %", 0.0, 10.0, 3.0, 0.5, disabled=True)
        
        st.info("Risk settings can be modified in config/trading_config.yaml")
    
    with tab3:
        st.subheader("Data Sources")
        
        st.checkbox("Yahoo Finance", value=True, disabled=True)
        st.checkbox("Finviz Elite", value=False, disabled=True)
        
        st.info("Data source configuration in .env file")

# Footer
st.markdown("---")
st.caption("💡 Tip: Start with Paper Trading mode to test strategies safely")
