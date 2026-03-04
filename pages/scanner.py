"""
Stock Scanner Dashboard Page
"""
import streamlit as st
import pandas as pd
from datetime import datetime

from connectors.yahoo_finance import YahooFinanceConnector
from connectors.finviz_scraper import FinvizScraper
from analysis.indicators import calculate_rsi, calculate_sma, calculate_atr
from core.logging_manager import get_logger

logger = get_logger("dashboard.scanner")


def render():
    """Render stock scanner page"""
    st.title("🔍 Stock Scanner")
    
    scanner_type = st.selectbox(
        "Select Scanner",
        ["Quick Momentum", "RSI Extremes", "Volume Surge", "Custom Scan"]
    )
    
    st.markdown("---")
    
    if scanner_type == "Quick Momentum":
        st.subheader("📈 Quick Momentum Scanner")
        st.write("Find stocks with recent price momentum")
        
        # User inputs
        col1, col2 = st.columns(2)
        with col1:
            min_gain = st.slider("Min Gain % (5 days)", 0, 50, 5)
        with col2:
            min_volume = st.number_input("Min Avg Volume", 100000, 10000000, 500000, 100000)
        
        if st.button("Run Momentum Scan", type="primary"):
            run_momentum_scan(min_gain, min_volume)
    
    elif scanner_type == "RSI Extremes":
        st.subheader("📊 RSI Extremes Scanner")
        st.write("Find overbought or oversold stocks")
        
        scan_mode = st.radio("Scan for", ["Oversold (RSI < 30)", "Overbought (RSI > 70)"])
        
        if st.button("Run RSI Scan", type="primary"):
            run_rsi_scan(scan_mode)
    
    elif scanner_type == "Volume Surge":
        st.subheader("📢 Volume Surge Scanner")
        st.write("Find stocks with unusual volume")
        
        volume_multiplier = st.slider("Volume vs Average", 1.5, 5.0, 2.0, 0.5)
        
        if st.button("Run Volume Scan", type="primary"):
            run_volume_scan(volume_multiplier)
    
    else:  # Custom Scan
        st.subheader("⚙️ Custom Scanner")
        st.write("Build your own scan criteria")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.write("**Price Filters**")
            min_price = st.number_input("Min Price $", 0.0, 1000.0, 5.0, 1.0)
            max_price = st.number_input("Max Price $", 0.0, 10000.0, 500.0, 10.0)
        
        with col2:
            st.write("**Technical Filters**")
            use_rsi = st.checkbox("RSI Filter")
            if use_rsi:
                rsi_min = st.slider("RSI Min", 0, 100, 30)
                rsi_max = st.slider("RSI Max", 0, 100, 70)
        
        if st.button("Run Custom Scan", type="primary"):
            st.info("Custom scan functionality - specify your watchlist below")


def run_momentum_scan(min_gain: float, min_volume: int):
    """Run momentum scanner"""
    with st.spinner("Scanning for momentum stocks..."):
        yf = YahooFinanceConnector()
        
        # Common tech stocks to scan (in practice, use a larger universe)
        watchlist = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 
                     'AMD', 'NFLX', 'DIS', 'BA', 'JPM', 'GS', 'PYPL']
        
        results = []
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for idx, symbol in enumerate(watchlist):
            status_text.text(f"Scanning {symbol}...")
            progress_bar.progress((idx + 1) / len(watchlist))
            
            try:
                data = yf.get_historical_data(symbol, days=10)
                
                if data.empty or len(data) < 5:
                    continue
                
                # Calculate 5-day gain
                latest_close = data.iloc[-1]['close']
                five_days_ago = data.iloc[-5]['close'] if len(data) >= 5 else data.iloc[0]['close']
                gain_pct = ((latest_close - five_days_ago) / five_days_ago) * 100
                
                # Check volume
                avg_volume = data['volume'].mean()
                
                if gain_pct >= min_gain and avg_volume >= min_volume:
                    results.append({
                        'Symbol': symbol,
                        'Price': f"${latest_close:.2f}",
                        '5-Day Gain %': f"{gain_pct:.2f}%",
                        'Avg Volume': f"{avg_volume:,.0f}"
                    })
            except Exception as e:
                logger.warning(f"Error scanning {symbol}: {e}")
                continue
        
        progress_bar.empty()
        status_text.empty()
        
        if results:
            st.success(f"Found {len(results)} stocks matching criteria")
            df = pd.DataFrame(results)
            st.dataframe(df, use_container_width=True)
        else:
            st.warning("No stocks found matching criteria. Try adjusting filters.")


def run_rsi_scan(scan_mode: str):
    """Run RSI scanner"""
    with st.spinner("Scanning for RSI extremes..."):
        yf = YahooFinanceConnector()
        
        watchlist = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META',
                     'AMD', 'NFLX', 'DIS', 'BA', 'JPM', 'GS', 'PYPL', 'INTC',
                     'CSCO', 'ORCL', 'CRM', 'ADBE', 'QCOM']
        
        results = []
        oversold = "Oversold" in scan_mode
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for idx, symbol in enumerate(watchlist):
            status_text.text(f"Scanning {symbol}...")
            progress_bar.progress((idx + 1) / len(watchlist))
            
            try:
                data = yf.get_historical_data(symbol, days=30)
                
                if data.empty or len(data) < 14:
                    continue
                
                # Calculate RSI
                data = calculate_rsi(data, period=14)
                
                if 'RSI_14' not in data.columns:
                    continue
                
                latest_rsi = data.iloc[-1]['RSI_14']
                latest_close = data.iloc[-1]['close']
                
                # Check RSI condition
                if oversold and latest_rsi < 30:
                    results.append({
                        'Symbol': symbol,
                        'Price': f"${latest_close:.2f}",
                        'RSI': f"{latest_rsi:.1f}",
                        'Signal': '🟢 Oversold'
                    })
                elif not oversold and latest_rsi > 70:
                    results.append({
                        'Symbol': symbol,
                        'Price': f"${latest_close:.2f}",
                        'RSI': f"{latest_rsi:.1f}",
                        'Signal': '🔴 Overbought'
                    })
                    
            except Exception as e:
                logger.warning(f"Error scanning {symbol}: {e}")
                continue
        
        progress_bar.empty()
        status_text.empty()
        
        if results:
            st.success(f"Found {len(results)} stocks with RSI extremes")
            df = pd.DataFrame(results)
            st.dataframe(df, use_container_width=True)
        else:
            st.warning("No stocks found with RSI extremes.")


def run_volume_scan(volume_multiplier: float):
    """Run volume surge scanner"""
    with st.spinner("Scanning for volume surges..."):
        yf = YahooFinanceConnector()
        
        watchlist = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META',
                     'AMD', 'NFLX', 'DIS', 'BA', 'JPM']
        
        results = []
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for idx, symbol in enumerate(watchlist):
            status_text.text(f"Scanning {symbol}...")
            progress_bar.progress((idx + 1) / len(watchlist))
            
            try:
                data = yf.get_historical_data(symbol, days=30)
                
                if data.empty or len(data) < 20:
                    continue
                
                # Calculate volume metrics
                avg_volume = data['volume'].iloc[:-1].mean()  # Exclude today
                latest_volume = data.iloc[-1]['volume']
                volume_ratio = latest_volume / avg_volume
                
                latest_close = data.iloc[-1]['close']
                
                if volume_ratio >= volume_multiplier:
                    results.append({
                        'Symbol': symbol,
                        'Price': f"${latest_close:.2f}",
                        'Volume Ratio': f"{volume_ratio:.2f}x",
                        'Volume': f"{latest_volume:,.0f}"
                    })
                    
            except Exception as e:
                logger.warning(f"Error scanning {symbol}: {e}")
                continue
        
        progress_bar.empty()
        status_text.empty()
        
        if results:
            st.success(f"Found {len(results)} stocks with volume surges")
            df = pd.DataFrame(results).sort_values('Volume Ratio', ascending=False)
            st.dataframe(df, use_container_width=True)
        else:
            st.warning("No stocks found with volume surges.")
