"""
Backtesting Dashboard Page
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta

from backtesting.backtest_engine import BacktestEngine
from strategies.swing_strategy import SwingTradingStrategy
from connectors.yahoo_finance import YahooFinanceConnector
from core.logging_manager import get_logger

logger = get_logger("dashboard.backtesting")


def render():
    """Render backtesting page"""
    st.title("📉 Strategy Backtesting")
    
    st.write("Test your strategies on historical data")
    
    # Configuration
    col1, col2 = st.columns(2)
    
    with col1:
        strategy = st.selectbox(
            "Strategy",
            ["Swing Trading", "Intraday Momentum (Coming Soon)"]
        )
        
        symbols_input = st.text_input(
            "Symbols (comma-separated)",
            "AAPL,MSFT,GOOGL,TSLA"
        )
    
    with col2:
        start_date = st.date_input(
            "Start Date",
            datetime.now() - timedelta(days=365)
        )
        
        end_date = st.date_input(
            "End Date",
            datetime.now()
        )
    
    initial_capital = st.number_input(
        "Initial Capital $",
        10000,
        1000000,
        100000,
        10000
    )
    
    st.markdown("---")
    
    if st.button("Run Backtest", type="primary"):
        run_backtest(strategy, symbols_input, start_date, end_date, initial_capital)


def run_backtest(strategy_name: str, symbols_str: str, start_date, end_date, initial_capital: float):
    """Run backtest with given parameters"""
    
    # Parse symbols
    symbols = [s.strip().upper() for s in symbols_str.split(',')]
    
    if not symbols:
        st.error("Please provide at least one symbol")
        return
    
    with st.spinner("Running backtest... This may take a minute."):
        try:
            # Fetch data
            st.info(f"Fetching historical data for {len(symbols)} symbols...")
            yf = YahooFinanceConnector()
            
            data = {}
            for symbol in symbols:
                df = yf.get_historical_data(
                    symbol,
                    start_date=start_date,
                    end_date=end_date
                )
                if not df.empty:
                    data[symbol] = df
            
            if not data:
                st.error("No data available for the specified symbols and date range")
                return
            
            st.success(f"Data loaded for {len(data)} symbols")
            
            # Initialize strategy
            if strategy_name == "Swing Trading":
                strategy = SwingTradingStrategy()
            else:
                st.error("Strategy not implemented yet")
                return
            
            # Run backtest
            st.info("Running backtest...")
            engine = BacktestEngine(initial_capital=initial_capital)
            result = engine.run(strategy, data, start_date, end_date)
            
            # Display results
            st.success("Backtest complete!")
            
            # Performance summary
            st.subheader("📊 Performance Summary")
            
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric(
                    "Total Return",
                    f"${result.total_return:,.2f}",
                    f"{result.total_return_pct:.2f}%"
                )
            
            with col2:
                st.metric(
                    "Win Rate",
                    f"{result.win_rate:.1f}%",
                    f"{result.winning_trades}/{result.total_trades}"
                )
            
            with col3:
                st.metric(
                    "Profit Factor",
                    f"{result.profit_factor:.2f}"
                )
            
            with col4:
                st.metric(
                    "Max Drawdown",
                    f"-{result.max_drawdown_pct:.2f}%",
                    f"${result.max_drawdown:,.2f}",
                    delta_color="inverse"
                )
            
            # Additional metrics
            st.markdown("---")
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.write("**Trade Statistics**")
                st.write(f"Total Trades: {result.total_trades}")
                st.write(f"Winning: {result.winning_trades}")
                st.write(f"Losing: {result.losing_trades}")
            
            with col2:
                st.write("**Average P&L**")
                st.write(f"Avg Win: ${result.avg_win:.2f}")
                st.write(f"Avg Loss: ${result.avg_loss:.2f}")
                avg_trade = result.total_return / result.total_trades if result.total_trades > 0 else 0
                st.write(f"Avg Trade: ${avg_trade:.2f}")
            
            with col3:
                st.write("**Risk Metrics**")
                st.write(f"Sharpe Ratio: {result.sharpe_ratio:.2f}")
                st.write(f"Max DD: {result.max_drawdown_pct:.2f}%")
                st.write(f"Final Capital: ${result.final_capital:,.2f}")
            
            st.markdown("---")
            
            # Equity curve
            if not result.equity_curve.empty:
                st.subheader("📈 Equity Curve")
                
                fig = go.Figure()
                
                fig.add_trace(go.Scatter(
                    x=result.equity_curve['date'],
                    y=result.equity_curve['equity'],
                    mode='lines',
                    name='Equity',
                    line=dict(color='#1f77b4', width=2)
                ))
                
                # Add initial capital line
                fig.add_hline(
                    y=initial_capital,
                    line_dash="dash",
                    line_color="gray",
                    annotation_text="Initial Capital"
                )
                
                fig.update_layout(
                    xaxis_title="Date",
                    yaxis_title="Equity ($)",
                    height=400,
                    showlegend=True,
                    hovermode='x unified'
                )
                
                st.plotly_chart(fig, use_container_width=True)
            
            # Trade list
            if result.trades:
                st.subheader("📋 Trade History")
                
                trades_data = []
                for trade in result.trades:
                    trades_data.append({
                        'Symbol': trade.symbol,
                        'Entry Date': trade.entry_date.strftime('%Y-%m-%d'),
                        'Entry Price': f"${trade.entry_price:.2f}",
                        'Exit Date': trade.exit_date.strftime('%Y-%m-%d') if trade.exit_date else 'Open',
                        'Exit Price': f"${trade.exit_price:.2f}" if trade.exit_price else 'N/A',
                        'Shares': trade.shares,
                        'P&L': f"${trade.pnl:.2f}",
                        'P&L %': f"{trade.pnl_percent:.2f}%",
                        'Status': trade.status
                    })
                
                trades_df = pd.DataFrame(trades_data)
                
                # Color code P&L
                def color_pnl(val):
                    if 'P&L' in val.name:
                        try:
                            num = float(val.str.replace('$', '').str.replace('%', ''))
                            if num > 0:
                                return ['color: green' for _ in val]
                            elif num < 0:
                                return ['color: red' for _ in val]
                        except:
                            pass
                    return ['' for _ in val]
                
                st.dataframe(trades_df, use_container_width=True)
                
                # Download button
                csv = trades_df.to_csv(index=False)
                st.download_button(
                    "Download Trade History CSV",
                    csv,
                    "backtest_trades.csv",
                    "text/csv",
                    key='download-csv'
                )
        
        except Exception as e:
            st.error(f"Backtest error: {e}")
            logger.error(f"Backtest failed: {e}", exc_info=True)
