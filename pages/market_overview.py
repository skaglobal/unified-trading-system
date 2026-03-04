"""
Market Overview Dashboard Page
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta

from connectors.yahoo_finance import YahooFinanceConnector
from analysis.indicators import calculate_rsi, calculate_sma
from core.logging_manager import get_logger

logger = get_logger("dashboard.market_overview")


def render():
    """Render market overview page"""
    st.title("📊 Market Overview")
    
    yf = YahooFinanceConnector()
    
    # Major indices
    st.subheader("Major Indices")
    
    indices = {
        'SPY': 'S&P 500',
        'QQQ': 'Nasdaq',
        'IWM': 'Russell 2000',
        'DIA': 'Dow Jones'
    }
    
    cols = st.columns(4)
    
    try:
        for idx, (symbol, name) in enumerate(indices.items()):
            with cols[idx]:
                data = yf.get_historical_data(symbol, days=5)
                if not data.empty:
                    latest = data.iloc[-1]
                    prev = data.iloc[-2] if len(data) > 1 else latest
                    
                    change = latest['close'] - prev['close']
                    change_pct = (change / prev['close']) * 100
                    
                    st.metric(
                        name,
                        f"${latest['close']:.2f}",
                        f"{change:+.2f} ({change_pct:+.2f}%)",
                        delta_color="normal"
                    )
                else:
                    st.metric(name, "N/A", "No data")
    except Exception as e:
        st.error(f"Error fetching index data: {e}")
        logger.error(f"Market overview error: {e}")
    
    st.markdown("---")
    
    # Market breadth
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📈 SPY Chart (30 Days)")
        try:
            spy_data = yf.get_historical_data('SPY', days=30)
            if not spy_data.empty:
                # Add moving averages
                spy_data = calculate_sma(spy_data, 20)
                
                fig = go.Figure()
                fig.add_trace(go.Candlestick(
                    x=spy_data['date'],
                    open=spy_data['open'],
                    high=spy_data['high'],
                    low=spy_data['low'],
                    close=spy_data['close'],
                    name='SPY'
                ))
                
                if 'SMA_20' in spy_data.columns:
                    fig.add_trace(go.Scatter(
                        x=spy_data['date'],
                        y=spy_data['SMA_20'],
                        name='SMA 20',
                        line=dict(color='orange', width=2)
                    ))
                
                fig.update_layout(
                    xaxis_title="Date",
                    yaxis_title="Price",
                    height=400,
                    showlegend=True,
                    xaxis_rangeslider_visible=False
                )
                
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No data available")
        except Exception as e:
            st.error(f"Error loading chart: {e}")
    
    with col2:
        st.subheader("📊 Market Indicators")
        
        try:
            spy_data = yf.get_historical_data('SPY', days=30)
            if not spy_data.empty:
                spy_data = calculate_rsi(spy_data, period=14)
                
                latest_rsi = spy_data.iloc[-1].get('RSI_14', 50) if 'RSI_14' in spy_data.columns else 50
                
                # RSI gauge
                st.metric("RSI (14)", f"{latest_rsi:.1f}")
                
                if latest_rsi > 70:
                    st.warning("🔴 Overbought")
                elif latest_rsi < 30:
                    st.success("🟢 Oversold")
                else:
                    st.info("⚪ Neutral")
                
                # Volume
                avg_volume = spy_data['volume'].mean()
                latest_volume = spy_data.iloc[-1]['volume']
                volume_ratio = latest_volume / avg_volume
                
                st.metric(
                    "Volume vs Avg",
                    f"{volume_ratio:.2f}x",
                    f"{latest_volume:,.0f}"
                )
                
                # Trend
                sma_20 = spy_data.iloc[-1].get('SMA_20', 0) if 'SMA_20' in spy_data.columns else 0
                latest_close = spy_data.iloc[-1]['close']
                
                if latest_close > sma_20:
                    st.success("📈 Above SMA(20)")
                else:
                    st.warning("📉 Below SMA(20)")
                    
        except Exception as e:
            st.error(f"Error calculating indicators: {e}")
    
    st.markdown("---")
    
    # Sector performance (simplified with major sector ETFs)
    st.subheader("🏭 Sector Performance (1 Day)")
    
    sectors = {
        'XLK': 'Technology',
        'XLF': 'Financials',
        'XLV': 'Healthcare',
        'XLE': 'Energy',
        'XLY': 'Consumer Disc.',
        'XLP': 'Consumer Staples',
        'XLI': 'Industrials',
        'XLU': 'Utilities'
    }
    
    sector_performance = []
    
    try:
        for symbol, name in sectors.items():
            data = yf.get_historical_data(symbol, days=5)
            if not data.empty and len(data) >= 2:
                latest = data.iloc[-1]
                prev = data.iloc[-2]
                change_pct = ((latest['close'] - prev['close']) / prev['close']) * 100
                sector_performance.append({
                    'Sector': name,
                    'Symbol': symbol,
                    'Change %': change_pct
                })
        
        if sector_performance:
            df = pd.DataFrame(sector_performance).sort_values('Change %', ascending=False)
            
            # Create bar chart
            fig = go.Figure()
            colors = ['green' if x > 0 else 'red' for x in df['Change %']]
            
            fig.add_trace(go.Bar(
                x=df['Change %'],
                y=df['Sector'],
                orientation='h',
                marker_color=colors,
                text=df['Change %'].apply(lambda x: f"{x:+.2f}%"),
                textposition='auto'
            ))
            
            fig.update_layout(
                xaxis_title="Change %",
                yaxis_title="Sector",
                height=400,
                showlegend=False
            )
            
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No sector data available")
            
    except Exception as e:
        st.error(f"Error loading sector data: {e}")
    
    # Market summary
    st.markdown("---")
    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
