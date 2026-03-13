"""
Intraday Trading Guidance Module
=================================
Educational decision-support tool for real-time intraday analysis.

Sub-modules
-----------
market_data_provider  — Abstract data-provider interface + IBKR implementation
indicator_engine      — Real-time indicator computation (VWAP, RSI, MACD, ATR …)
scoring_engine        — Weighted bull/bear scoring with configurable YAML weights
guidance_engine       — Entry / stop / target / R-R guidance output
alert_engine          — Real-time alert generation
paper_trade_logger    — Event logging for paper-trade and back-test modes
backtest_mode         — Historical simulation using 1-min IBKR data

DISCLAIMER
----------
All output is *probability guidance*, not guaranteed prediction.
This module is for educational decision support only — not automated execution.
"""
