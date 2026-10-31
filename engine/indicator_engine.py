"""
Trend-Investorr: Technical Indicator Computation Engine

Centralized, pure-math computation for ALL technical indicators.
No external API calls, no database access — just numpy math.

This is the SINGLE SOURCE OF TRUTH for indicator formulas.
StaticRuleAnalyzer and other consumers import from here.

Indicators:
    Trend:       SMA(20/50/200), EMA(20/50), Supertrend
    Momentum:    RSI(14), MACD(12,26,9), ADX(14)
    Volatility:  ATR(14), Bollinger Bands(20,2)
    Volume:      OBV, VWAP
    Structure:   Pivot Points, Swing-based Support/Resistance, 52-Week H/L
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
from loguru import logger

from data.models.market_data import Candle, TechnicalIndicators


class IndicatorEngine:
    """
    Pure-math computation engine for all technical indicators.

    All methods are static — no instance state needed.

    Usage:
        from engine.indicator_engine import IndicatorEngine

        indicators = IndicatorEngine.compute_all(candles, stock_id, target_date)
        # indicators.rsi_14, indicators.atr_14, etc.
    """

    # ── Minimum data requirements ────────────────────────
    MIN_CANDLES_RSI = 15       # RSI(14) needs 15+ candles
    MIN_CANDLES_EMA = 50       # EMA(50) needs 50+ candles
    MIN_CANDLES_SMA = 200      # SMA(200) needs 200+ candles (optional)
    MIN_CANDLES_MACD = 35      # MACD needs EMA(26) + 9 signal = 35+
    MIN_CANDLES_SUPERTREND = 15  # Supertrend(10,3) needs 15+ candles
    MIN_CANDLES_ADX = 28       # ADX(14) needs 28+ candles

    @staticmethod
    def compute_all(
        candles: list[Candle],
        stock_id: int,
        target_date: date,
    ) -> TechnicalIndicators:
        """
        Compute ALL indicators from a list of daily candles.

        Requires at least 50 candles for meaningful results.
        Missing indicators (due to insufficient data) default to 0.

        Args:
            candles: Daily candles in chronological order (oldest first).
            stock_id: Database stock ID.
            target_date: The date these indicators are computed for.

        Returns:
            TechnicalIndicators dataclass with all computed values.
        """
        result = TechnicalIndicators(stock_id=stock_id, date=target_date)

        if not candles or len(candles) < 14:
            return result

        closes = np.array([c.close for c in candles], dtype=np.float64)
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)

        try:
            # ── Trend ──
            if len(closes) >= 20:
                sma20 = IndicatorEngine.compute_sma(closes, 20)
                result.sma_20 = round(float(sma20[-1]), 2)
            if len(closes) >= 50:
                sma50 = IndicatorEngine.compute_sma(closes, 50)
                result.sma_50 = round(float(sma50[-1]), 2)
            if len(closes) >= 200:
                sma200 = IndicatorEngine.compute_sma(closes, 200)
                result.sma_200 = round(float(sma200[-1]), 2)

            if len(closes) >= 20:
                ema20 = IndicatorEngine.compute_ema(closes, 20)
                result.ema_20 = round(float(ema20[-1]), 2)
            if len(closes) >= 50:
                ema50 = IndicatorEngine.compute_ema(closes, 50)
                result.ema_50 = round(float(ema50[-1]), 2)

            # Supertrend
            if len(closes) >= IndicatorEngine.MIN_CANDLES_SUPERTREND:
                st_val, st_dir = IndicatorEngine.compute_supertrend(
                    highs, lows, closes, period=10, multiplier=3.0,
                )
                result.supertrend = round(float(st_val[-1]), 2)
                result.supertrend_direction = int(st_dir[-1])

            # ── Momentum ──
            if len(closes) >= IndicatorEngine.MIN_CANDLES_RSI:
                rsi = IndicatorEngine.compute_rsi(closes, 14)
                result.rsi_14 = round(float(rsi[-1]), 2)
                
                rsi_10_arr = IndicatorEngine.compute_rsi(closes, 10)
                result.rsi_10 = round(float(rsi_10_arr[-1]), 2)

            if len(closes) >= IndicatorEngine.MIN_CANDLES_MACD:
                macd_line, macd_signal, macd_hist = IndicatorEngine.compute_macd(closes)
                result.macd_line = round(float(macd_line[-1]), 2)
                result.macd_signal = round(float(macd_signal[-1]), 2)
                result.macd_histogram = round(float(macd_hist[-1]), 4)

            if len(closes) >= IndicatorEngine.MIN_CANDLES_ADX:
                adx = IndicatorEngine.compute_adx(highs, lows, closes, 14)
                result.adx_14 = round(float(adx[-1]), 2)

            # ── Volatility ──
            atr = IndicatorEngine.compute_atr(highs, lows, closes, 14)
            result.atr_14 = round(float(atr[-1]), 2)

            if len(closes) >= 20:
                bb_upper, bb_mid, bb_lower = IndicatorEngine.compute_bollinger(
                    closes, 20, 2.0,
                )
                result.bollinger_upper = round(float(bb_upper[-1]), 2)
                result.bollinger_lower = round(float(bb_lower[-1]), 2)
                mid = bb_mid[-1]
                if mid > 0:
                    result.bollinger_width = round(
                        float((bb_upper[-1] - bb_lower[-1]) / mid), 4,
                    )

            # ── Volume ──
            obv = IndicatorEngine.compute_obv(closes, volumes)
            result.obv = int(obv[-1])

            vwap = IndicatorEngine.compute_vwap(highs, lows, closes, volumes)
            result.vwap_daily = round(float(vwap[-1]), 2)

            # Delivery % from the latest candle (if present)
            if hasattr(candles[-1], "delivery_pct") and candles[-1].delivery_pct > 0:
                result.delivery_pct = round(candles[-1].delivery_pct, 2)

            # ── Structure ──
            # Pivot points from the latest (yesterday's) candle
            last = candles[-1]
            pp, r1, r2, s1, s2 = IndicatorEngine.compute_pivots(
                last.high, last.low, last.close,
            )
            result.pivot_point = round(pp, 2)
            result.pivot_r1 = round(r1, 2)
            result.pivot_r2 = round(r2, 2)
            result.pivot_s1 = round(s1, 2)
            result.pivot_s2 = round(s2, 2)

            # Support/Resistance from swing detection
            if len(candles) >= 20:
                support, resistance = IndicatorEngine.compute_support_resistance(
                    highs, lows, closes, window=5,
                )
                result.support_level = round(support, 2)
                result.resistance_level = round(resistance, 2)

            # 52-week high/low (use all available data, up to 252 trading days)
            result.high_52w = round(float(np.max(highs)), 2)
            result.low_52w = round(float(np.min(lows)), 2)

        except Exception as e:
            logger.error(f"Indicator computation error for stock_id={stock_id}: {e}")

        return result

    # ══════════════════════════════════════════════════════
    # ── Individual Indicator Methods ─────────────────────
    # ══════════════════════════════════════════════════════

    @staticmethod
    def compute_sma(data: np.ndarray, period: int) -> np.ndarray:
        """
        Simple Moving Average.

        SMA = sum(last N closes) / N
        """
        if len(data) < period:
            return data.copy()

        kernel = np.ones(period) / period
        sma = np.convolve(data, kernel, mode="valid")

        # Pad beginning with NaN-equivalent (first SMA value)
        padded = np.full(len(data), sma[0])
        padded[period - 1:] = sma
        return padded

    @staticmethod
    def compute_ema(data: np.ndarray, period: int) -> np.ndarray:
        """
        Exponential Moving Average.

        EMA = close × multiplier + prev_ema × (1 - multiplier)
        multiplier = 2 / (period + 1)
        """
        if len(data) < period:
            return data.copy()

        multiplier = 2.0 / (period + 1)
        ema = np.zeros_like(data)
        ema[period - 1] = np.mean(data[:period])  # SMA seed

        for i in range(period, len(data)):
            ema[i] = data[i] * multiplier + ema[i - 1] * (1 - multiplier)

        ema[:period - 1] = ema[period - 1]  # Fill initial
        return ema

    @staticmethod
    def compute_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
        """
        Relative Strength Index using Wilder's smoothing.

        RSI = 100 - (100 / (1 + RS))
        RS = avg_gain / avg_loss over period bars
        """
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.zeros_like(gains)
        avg_loss = np.zeros_like(losses)

        if len(gains) < period:
            return np.full(len(closes), 50.0)

        avg_gain[period - 1] = np.mean(gains[:period])
        avg_loss[period - 1] = np.mean(losses[:period])

        for i in range(period, len(gains)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

        # Wilder RSI treats a zero average loss as a fully overbought reading
        # when gains exist.  The previous fallback used RS=1, which returned
        # RSI=50 for a continuously rising series and distorted every RSI
        # based filter on that edge case.
        rs = np.divide(
            avg_gain, avg_loss,
            out=np.full_like(avg_gain, np.inf),
            where=avg_loss != 0,
        )
        rsi = 100 - (100 / (1 + rs))
        flat = (avg_gain == 0) & (avg_loss == 0)
        rsi[flat] = 50.0

        rsi_full = np.full(len(closes), 50.0)
        rsi_full[1:] = rsi
        return rsi_full

    @staticmethod
    def compute_macd(
        closes: np.ndarray,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        MACD (Moving Average Convergence Divergence).

        MACD Line = EMA(12) - EMA(26)
        Signal Line = EMA(9) of MACD Line
        Histogram = MACD Line - Signal Line

        Returns:
            (macd_line, signal_line, histogram) — all same length as closes.
        """
        ema_fast = IndicatorEngine.compute_ema(closes, fast)
        ema_slow = IndicatorEngine.compute_ema(closes, slow)
        macd_line = ema_fast - ema_slow

        # Signal line = EMA of MACD line
        signal_line = IndicatorEngine.compute_ema(macd_line, signal)
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    @staticmethod
    def compute_atr(
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14,
    ) -> np.ndarray:
        """
        Average True Range.

        True Range = max(H-L, |H-prevC|, |L-prevC|)
        ATR = Wilder's smoothed average of TR over period
        """
        if len(highs) < 2:
            return np.zeros_like(highs)

        # True Range
        hl = highs[1:] - lows[1:]
        hc = np.abs(highs[1:] - closes[:-1])
        lc = np.abs(lows[1:] - closes[:-1])
        tr = np.maximum(hl, np.maximum(hc, lc))

        atr = np.zeros(len(highs))
        if len(tr) < period:
            atr[1:] = tr
            return atr

        # Initial ATR = simple average of first 'period' TRs
        atr[period] = np.mean(tr[:period])

        # Wilder's smoothing
        for i in range(period + 1, len(highs)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period

        # Fill initial values
        atr[:period] = atr[period]
        return atr

    @staticmethod
    def compute_bollinger(
        closes: np.ndarray,
        period: int = 20,
        std_dev: float = 2.0,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Bollinger Bands.

        Middle = SMA(period)
        Upper = SMA + std_dev × σ
        Lower = SMA - std_dev × σ

        Returns:
            (upper, middle, lower) bands.
        """
        middle = IndicatorEngine.compute_sma(closes, period)

        # Rolling standard deviation
        rolling_std = np.zeros_like(closes)
        for i in range(period - 1, len(closes)):
            rolling_std[i] = np.std(closes[i - period + 1: i + 1], ddof=0)
        rolling_std[:period - 1] = rolling_std[period - 1]

        upper = middle + std_dev * rolling_std
        lower = middle - std_dev * rolling_std

        return upper, middle, lower

    @staticmethod
    def compute_supertrend(
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 10,
        multiplier: float = 3.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Supertrend Indicator.

        Upper Band = (H+L)/2 + multiplier × ATR
        Lower Band = (H+L)/2 - multiplier × ATR
        Direction: 1 = bullish (price above lower band), -1 = bearish

        Returns:
            (supertrend_values, direction) — direction is +1 or -1.
        """
        n = len(closes)
        atr = IndicatorEngine.compute_atr(highs, lows, closes, period)
        hl2 = (highs + lows) / 2.0

        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        supertrend = np.zeros(n)
        direction = np.ones(n)  # Start bullish

        supertrend[0] = lower_band[0]

        for i in range(1, n):
            # Adjust bands: prevent them from moving against the trend
            if lower_band[i] > lower_band[i - 1] or closes[i - 1] < lower_band[i - 1]:
                pass  # Keep current lower_band
            else:
                lower_band[i] = lower_band[i - 1]

            if upper_band[i] < upper_band[i - 1] or closes[i - 1] > upper_band[i - 1]:
                pass  # Keep current upper_band
            else:
                upper_band[i] = upper_band[i - 1]

            # Determine direction
            if direction[i - 1] == 1:  # Was bullish
                if closes[i] < lower_band[i]:
                    direction[i] = -1
                    supertrend[i] = upper_band[i]
                else:
                    direction[i] = 1
                    supertrend[i] = lower_band[i]
            else:  # Was bearish
                if closes[i] > upper_band[i]:
                    direction[i] = 1
                    supertrend[i] = lower_band[i]
                else:
                    direction[i] = -1
                    supertrend[i] = upper_band[i]

        return supertrend, direction

    @staticmethod
    def compute_adx(
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14,
    ) -> np.ndarray:
        """
        Average Directional Index.

        Measures trend strength (0-100). ADX > 25 = strong trend.
        Uses +DI and -DI directional movement indicators.
        """
        n = len(closes)
        if n < period * 2:
            return np.full(n, 0.0)

        # Directional Movement
        up_move = highs[1:] - highs[:-1]
        down_move = lows[:-1] - lows[1:]

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        atr = IndicatorEngine.compute_atr(highs, lows, closes, period)
        atr_trimmed = atr[1:]  # Align with DM arrays

        # Smooth DM with Wilder's method
        smooth_plus = np.zeros_like(plus_dm)
        smooth_minus = np.zeros_like(minus_dm)

        if len(plus_dm) < period:
            return np.full(n, 0.0)

        smooth_plus[period - 1] = np.sum(plus_dm[:period])
        smooth_minus[period - 1] = np.sum(minus_dm[:period])

        for i in range(period, len(plus_dm)):
            smooth_plus[i] = smooth_plus[i - 1] - (smooth_plus[i - 1] / period) + plus_dm[i]
            smooth_minus[i] = smooth_minus[i - 1] - (smooth_minus[i - 1] / period) + minus_dm[i]

        # +DI and -DI
        plus_di = np.divide(
            smooth_plus * 100, atr_trimmed,
            out=np.zeros_like(smooth_plus), where=atr_trimmed != 0,
        )
        minus_di = np.divide(
            smooth_minus * 100, atr_trimmed,
            out=np.zeros_like(smooth_minus), where=atr_trimmed != 0,
        )

        # DX
        di_sum = plus_di + minus_di
        dx = np.divide(
            np.abs(plus_di - minus_di) * 100, di_sum,
            out=np.zeros_like(di_sum), where=di_sum != 0,
        )

        # ADX = Wilder's smooth of DX
        adx = np.zeros_like(dx)
        if len(dx) >= period * 2:
            adx[period * 2 - 2] = np.mean(dx[period - 1: period * 2 - 1])
            for i in range(period * 2 - 1, len(dx)):
                adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

        # Pad back to original length
        adx_full = np.full(n, 0.0)
        adx_full[1:] = adx
        return adx_full

    @staticmethod
    def compute_obv(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
        """
        On-Balance Volume.

        OBV += volume if close > prev_close
        OBV -= volume if close < prev_close
        OBV unchanged if close == prev_close
        """
        obv = np.zeros_like(volumes)
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv[i] = obv[i - 1] + volumes[i]
            elif closes[i] < closes[i - 1]:
                obv[i] = obv[i - 1] - volumes[i]
            else:
                obv[i] = obv[i - 1]
        return obv

    @staticmethod
    def compute_vwap(
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
    ) -> np.ndarray:
        """
        Volume-Weighted Average Price.

        VWAP = cumsum(TP × Volume) / cumsum(Volume)
        TP = (High + Low + Close) / 3
        """
        typical_price = (highs + lows + closes) / 3.0
        cum_tp_vol = np.cumsum(typical_price * volumes)
        cum_vol = np.cumsum(volumes)

        vwap = np.divide(
            cum_tp_vol, cum_vol,
            out=np.zeros_like(cum_tp_vol), where=cum_vol != 0,
        )
        return vwap

    @staticmethod
    def compute_pivots(
        high: float,
        low: float,
        close: float,
    ) -> tuple[float, float, float, float, float]:
        """
        Classic Pivot Points from a single candle (typically yesterday's).

        PP = (H + L + C) / 3
        R1 = 2×PP - L
        R2 = PP + (H - L)
        S1 = 2×PP - H
        S2 = PP - (H - L)

        Returns:
            (pivot, r1, r2, s1, s2)
        """
        pp = (high + low + close) / 3.0
        r1 = 2 * pp - low
        r2 = pp + (high - low)
        s1 = 2 * pp - high
        s2 = pp - (high - low)
        return pp, r1, r2, s1, s2

    @staticmethod
    def compute_support_resistance(
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        window: int = 5,
    ) -> tuple[float, float]:
        """
        Compute support and resistance from swing highs/lows.

        A swing high: a point where high[i] > high[i-w:i] and high[i+1:i+w+1]
        A swing low: a point where low[i] < low[i-w:i] and low[i+1:i+w+1]

        Takes the most recent swing low as support, most recent swing high as resistance.

        Args:
            highs, lows, closes: Price arrays.
            window: Number of bars on each side to confirm swing.

        Returns:
            (support, resistance) — last swing low and last swing high.
        """
        n = len(highs)
        swing_highs: list[float] = []
        swing_lows: list[float] = []

        for i in range(window, n - window):
            # Swing high: highest in the window
            if highs[i] == np.max(highs[i - window: i + window + 1]):
                swing_highs.append(float(highs[i]))

            # Swing low: lowest in the window
            if lows[i] == np.min(lows[i - window: i + window + 1]):
                swing_lows.append(float(lows[i]))

        # Take the most recent swing points
        support = swing_lows[-1] if swing_lows else float(np.min(lows[-20:]))
        resistance = swing_highs[-1] if swing_highs else float(np.max(highs[-20:]))

        return support, resistance
