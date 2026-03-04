"""
Swing Trading Strategy - Multi-day position holds (2-10 days).

Based on momentum, trend following, and technical breakouts.
"""

from datetime import datetime
from typing import Dict, List

import pandas as pd

from analysis.indicators import TechnicalIndicators
from strategies.base_strategy import BaseStrategy, SignalStrength, SignalType, TradingSignal


class SwingTradingStrategy(BaseStrategy):
    """
    Swing trading strategy for 2-10 day holds.
    
    Entry criteria:
    - Strong momentum (RSI 50-70)
    - Trend alignment (price above MAs)
    - Volume confirmation
    - Breakout or pullback setup
    
    Exit criteria:
    - Take profit: 5-15% depending on volatility
    - Stop loss: 1.5x ATR or below key support
    """
    
    def __init__(self, config=None, logger=None):
        """Initialize swing trading strategy."""
        super().__init__("Swing Trading", config, logger)
        
        self.indicators = TechnicalIndicators(logger)
        
        # Strategy parameters
        self.min_volume = 1000000  # Higher volume for swing
        self.min_price = 10.0
        self.max_price = 300.0
        self.max_positions = 8
        
        # Technical parameters
        self.rsi_min = 50
        self.rsi_max = 70
        self.min_trend_strength = 60
        self.atr_stop_multiplier = 1.5
        self.profit_target_multiplier = 2.5  # Risk:reward ratio
    
    def generate_signals(self, data: Dict[str, pd.DataFrame]) -> List[TradingSignal]:
        """Generate swing trading signals."""
        signals = []
        
        # Filter universe
        filtered_data = self.filter_universe(data)
        
        for symbol, df in filtered_data.items():
            try:
                # Ensure indicators are present
                if 'RSI_14' not in df.columns:
                    df = self.indicators.add_all_indicators(df)
                
                # Generate signal
                signal = self._analyze_symbol(symbol, df)
                
                if signal:
                    self.signals_generated += 1
                    
                    # Validate signal
                    if self.validate_signal(signal, df):
                        signals.append(signal)
                        self.signals_validated += 1
                        
            except Exception as e:
                self.logger.error(f"Error analyzing {symbol}: {e}")
        
        # Sort by strength and limit to top signals
        signals.sort(key=lambda x: (x.strength.value, x.price), reverse=True)
        
        return signals[:self.max_positions]
    
    def _analyze_symbol(self, symbol: str, df: pd.DataFrame) -> Optional[TradingSignal]:
        """Analyze a single symbol for swing trade opportunities."""
        if len(df) < 200:
            return None
        
        # Get current values
        current_price = df['Close'].iloc[-1]
        rsi = df['RSI_14'].iloc[-1]
        atr = df['ATR_20'].iloc[-1]
        
        # Check trend strength
        trend_strength = self.indicators.calculate_trend_strength(df)
        
        # Check for valid RSI range
        if not (self.rsi_min <= rsi <= self.rsi_max):
            return None
        
        # Check trend strength
        if trend_strength < self.min_trend_strength:
            return None
        
        # Detect setup type
        is_breakout = self.indicators.detect_breakout(df, lookback=20)
        is_pullback = self.indicators.detect_pullback(df, ma_period=20)
        
        if not (is_breakout or is_pullback):
            return None
        
        # Determine signal strength
        strength = self._calculate_signal_strength(df, rsi, trend_strength)
        
        # Calculate stops and targets
        stop_loss = current_price - (atr * self.atr_stop_multiplier)
        take_profit = current_price + (atr * self.atr_stop_multiplier * self.profit_target_multiplier)
        
        # Build reason
        setup_type = "breakout" if is_breakout else "pullback"
        reason = (
            f"Swing {setup_type}: RSI={rsi:.1f}, Trend={trend_strength:.0f}%, "
            f"R:R={self.profit_target_multiplier}:1"
        )
        
        # Create signal
        signal = TradingSignal(
            symbol=symbol,
            signal_type=SignalType.BUY,
            strength=strength,
            price=current_price,
            timestamp=datetime.now(),
            indicators={
                'rsi': rsi,
                'trend_strength': trend_strength,
                'atr': atr,
                'setup_type': setup_type
            },
            reason=reason,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size_pct=2.5
        )
        
        return signal
    
    def _calculate_signal_strength(
        self,
        df: pd.DataFrame,
        rsi: float,
        trend_strength: float
    ) -> SignalStrength:
        """Calculate signal strength based on multiple factors."""
        score = 0
        
        # RSI score (0-30)
        if 55 <= rsi <= 65:
            score += 30
        elif 50 <= rsi <= 70:
            score += 20
        else:
            score += 10
        
        # Trend strength score (0-40)
        if trend_strength >= 80:
            score += 40
        elif trend_strength >= 60:
            score += 30
        else:
            score += 20
        
        # Volume confirmation (0-15)
        if 'Volume_Ratio' in df.columns:
            vol_ratio = df['Volume_Ratio'].iloc[-1]
            if vol_ratio > 1.5:
                score += 15
            elif vol_ratio > 1.2:
                score += 10
            else:
                score += 5
        
        # MA alignment (0-15)
        if 'Bullish_Alignment' in df.columns and df['Bullish_Alignment'].iloc[-1] == 1:
            score += 15
        
        # Map score to strength
        if score >= 75:
            return SignalStrength.STRONG
        elif score >= 60:
            return SignalStrength.MODERATE
        else:
            return SignalStrength.WEAK
    
    def validate_signal(self, signal: TradingSignal, df: pd.DataFrame) -> bool:
        """Validate swing trading signal."""
        # Check risk:reward ratio
        if signal.stop_loss and signal.take_profit:
            risk = signal.price - signal.stop_loss
            reward = signal.take_profit - signal.price
            
            if reward / risk < 2.0:  # Minimum 2:1 R:R
                return False
        
        # Check for recent gap down (avoid catching falling knives)
        if len(df) >= 2:
            prev_close = df['Close'].iloc[-2]
            gap_pct = ((signal.price - prev_close) / prev_close) * 100
            
            if gap_pct < -5:  # More than 5% gap down
                return False
        
        # Check volatility isn't excessive
        if 'HV_20' in df.columns:
            hv = df['HV_20'].iloc[-1]
            if hv > 100:  # More than 100% annualized volatility
                return False
        
        # Weak signals require perfect conditions
        if signal.strength == SignalStrength.WEAK:
            if signal.indicators.get('trend_strength', 0) < 70:
                return False
        
        return True
