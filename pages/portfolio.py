"""
Portfolio Dashboard Page
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime

from connectors.ibkr_connector import IBKRConnector
from core.logging_manager import get_logger
from core.config_manager import get_config_manager

logger = get_logger("dashboard.portfolio")


def render():
    """Render portfolio page"""
    st.title("💼 Portfolio")
    
    config = get_config_manager()
    
    if not config.is_paper_trading():
        st.warning("⚠️ LIVE TRADING MODE - Displaying real positions")
    else:
        st.info("📝 Paper Trading Mode - Simulated positions")
    
    st.markdown("---")
    
    # Connect to IBKR  
    try:
        with st.spinner("Connecting to IBKR..."):
            ibkr = IBKRConnector()
            
            if ibkr.connect():
                st.success("✅ Connected to IBKR")
                
                # Get positions
                positions = ibkr.get_positions()
                
                if positions:
                    display_positions(positions, ibkr)
                else:
                    st.info("No open positions")
                
                # Account summary
                display_account_summary(ibkr)
                
                ibkr.disconnect()
            else:
                st.error("❌ Failed to connect to IBKR")
                st.info("Make sure TWS/Gateway is running and configured correctly")
                display_mock_portfolio()
                
    except Exception as e:
        st.error(f"Connection error: {e}")
        logger.error(f"Portfolio page error: {e}")
        st.info("Displaying example portfolio layout")
        display_mock_portfolio()


def display_positions(positions: list, ibkr: IBKRConnector):
    """Display current positions"""
    st.subheader("📊 Current Positions")
    
    positions_data = []
    total_value = 0
    total_pnl = 0
    
    for pos in positions:
        try:
            # Get current price
            current_price = ibkr.get_last_price(pos['symbol'])
            
            if current_price:
                market_value = pos['position'] * current_price
                cost_basis = pos['avgCost'] * pos['position']
                pnl = market_value - cost_basis
                pnl_pct = (pnl / cost_basis * 100) if cost_basis != 0 else 0
                
                total_value += market_value
                total_pnl += pnl
                
                positions_data.append({
                    'Symbol': pos['symbol'],
                    'Shares': pos['position'],
                    'Avg Cost': f"${pos['avgCost']:.2f}",
                    'Current Price': f"${current_price:.2f}",
                    'Market Value': f"${market_value:,.2f}",
                    'P&L': f"${pnl:,.2f}",
                    'P&L %': f"{pnl_pct:+.2f}%"
                })
        except Exception as e:
            logger.error(f"Error processing position {pos['symbol']}: {e}")
    
    if positions_data:
        # Summary metrics
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("Total Positions", len(positions_data))
        
        with col2:
            st.metric("Market Value", f"${total_value:,.2f}")
        
        with col3:
            st.metric(
                "Total P&L",
                f"${total_pnl:,.2f}",
                delta_color="normal"
            )
        
        # Positions table
        df = pd.DataFrame(positions_data)
        st.dataframe(df, use_container_width=True)
        
        # P&L chart
        st.subheader("📈 P&L by Position")
        
        fig = go.Figure()
        
        pnl_values = [float(p['P&L'].replace('$', '').replace(',', '')) for p in positions_data]
        colors = ['green' if x > 0 else 'red' for x in pnl_values]
        
        fig.add_trace(go.Bar(
            x=[p['Symbol'] for p in positions_data],
            y=pnl_values,
            marker_color=colors,
            text=[p['P&L'] for p in positions_data],
            textposition='auto'
        ))
        
        fig.update_layout(
            xaxis_title="Symbol",
            yaxis_title="P&L ($)",
            height=400,
            showlegend=False
        )
        
        st.plotly_chart(fig, use_container_width=True)


def display_account_summary(ibkr: IBKRConnector):
    """Display account summary"""
    st.markdown("---")
    st.subheader("💰 Account Summary")
    
    try:
        account_info = ibkr.get_account_values()
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            nav = account_info.get('NetLiquidation', 0)
            st.metric("Net Liquidation", f"${float(nav):,.2f}")
        
        with col2:
            cash = account_info.get('CashBalance', 0)
            st.metric("Cash Balance", f"${float(cash):,.2f}")
        
        with col3:
            buying_power = account_info.get('BuyingPower', 0)
            st.metric("Buying Power", f"${float(buying_power):,.2f}")
        
        with col4:
            unrealized = account_info.get('UnrealizedPnL', 0)
            st.metric("Unrealized P&L", f"${float(unrealized):,.2f}")
        
    except Exception as e:
        st.error(f"Error loading account summary: {e}")
        logger.error(f"Account summary error: {e}")


def display_mock_portfolio():
    """Display mock portfolio for demonstration"""
    st.subheader("📊 Example Portfolio (Mock Data)")
    
    mock_positions = [
        {
            'Symbol': 'AAPL',
            'Shares': 100,
            'Avg Cost': '$175.50',
            'Current Price': '$182.30',
            'Market Value': '$18,230.00',
            'P&L': '$680.00',
            'P&L %': '+3.87%'
        },
        {
            'Symbol': 'MSFT',
            'Shares': 50,
            'Avg Cost': '$380.20',
            'Current Price': '$395.75',
            'Market Value': '$19,787.50',
            'P&L': '$777.50',
            'P&L %': '+4.09%'
        },
        {
            'Symbol': 'GOOGL',
            'Shares': 75,
            'Avg Cost': '$142.80',
            'Current Price': '$138.50',
            'Market Value': '$10,387.50',
            'P&L': '-$322.50',
            'P&L %': '-3.01%'
        }
    ]
    
    # Summary
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Total Positions", "3")
    
    with col2:
        st.metric("Market Value", "$48,405.00")
    
    with col3:
        st.metric("Total P&L", "$1,135.00", "+2.40%")
    
    # Table
    df = pd.DataFrame(mock_positions)
    st.dataframe(df, use_container_width=True)
    
    st.info("💡 Connect to IBKR to see real positions")
