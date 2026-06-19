"""
Trend-Investorr: Signal Generator

Converts volume scan results into actionable trading signals by
combining volume spike data with sector context and filtering.

Pipeline:
    VolumeScanner (spikes)
        → SectorClassifier (sector context)
        → StockFilter (eligibility)
        → SignalGenerator (scoring + Signal creation)
        → Analyzer (validation)

The signal generator is the bridge between raw detection and
the analysis layer. It scores each spike candidate and produces
Signal dataclasses that analyzers can validate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, AuditEventType, SignalStatus, SignalType
from core.security.audit import AuditTrail
from data.models.market_data import Signal, SignalStrength
from engine.filters import FilterResult
from engine.volume_scanner import VolumeScanResult
from engine.sector_classifier import SectorHeat


class SignalGenerator:
    """
    Generates and scores trading signals from volume spike detections.

    Scoring Model (0.0 - 1.0):
        - Volume ratio factor:     40% weight
        - Sector heat factor:      25% weight
        - Delivery % factor:       15% weight
        - Price momentum factor:   10% weight
        - Advance/decline factor:  10% weight

    Usage:
        gen = SignalGenerator(audit=audit_trail)
        signal = gen.generate(spike_result, sector_heat, delivery_pct)
    """

    # Score weights
    WEIGHT_VOLUME = 0.40
    WEIGHT_SECTOR = 0.25
    WEIGHT_DELIVERY = 0.15
    WEIGHT_MOMENTUM = 0.10
    WEIGHT_BREADTH = 0.10

    # Minimum score to generate a signal
    MIN_SIGNAL_SCORE = 0.35

    def __init__(self, audit: AuditTrail | None = None):
        self._audit = audit
        self._signals_generated: int = 0
        self._signals_rejected: int = 0

    def generate(
        self,
        spike: VolumeScanResult,
        sector_heat: SectorHeat | None = None,
        delivery_pct: float = 0.0,
        price_change_pct: float = 0.0,
    ) -> Signal | None:
        """
        Generate a trading signal from a volume spike.

        Args:
            spike: Volume scan result showing the spike.
            sector_heat: Sector-level metrics (optional, improves scoring).
            delivery_pct: Delivery percentage for the stock today (0-100).
            price_change_pct: Intraday price change percentage.

        Returns:
            Signal if score exceeds minimum threshold, None otherwise.
        """
        # Calculate composite score
        score = self._calculate_score(spike, sector_heat, delivery_pct, price_change_pct)

        if score < self.MIN_SIGNAL_SCORE:
            self._signals_rejected += 1
            logger.debug(
                f"Signal rejected for {spike.symbol}: "
                f"score {score:.2f} < min {self.MIN_SIGNAL_SCORE}"
            )
            return None

        # Determine signal type based on context
        signal_type = self._determine_signal_type(spike, price_change_pct, sector_heat)

        signal = Signal(
            symbol=spike.symbol,
            token=spike.token,
            signal_type=signal_type,
            strength=spike.strength,
            score=score,
            price=spike.price,
            sector=sector_heat.sector_name if sector_heat else "",
            volume_current=spike.cumulative_volume,
            volume_avg=spike.avg_volume_30d,
            volume_ratio=spike.time_normalized_ratio,
            status=SignalStatus.PENDING,
            metadata={
                "raw_volume_ratio": round(spike.raw_ratio, 2),
                "normalized_volume_ratio": round(spike.time_normalized_ratio, 2),
                "delivery_pct": delivery_pct,
                "price_change_pct": round(price_change_pct, 2),
                "sector_heat_score": round(sector_heat.heat_score, 2) if sector_heat else 0,
                # Enriched tick data (previously unused)
                "exchange_vwap": round(spike.exchange_vwap, 2),
                "buy_sell_ratio": round(spike.buy_sell_ratio, 2),
                "bid_ask_spread": round(spike.bid_ask_spread, 2),
                "gap_pct": round(spike.gap_pct, 2),
                "score_breakdown": self._get_score_breakdown(
                    spike, sector_heat, delivery_pct, price_change_pct
                ),
            },
        )

        self._signals_generated += 1

        # Audit log
        if self._audit:
            self._audit.log(
                AuditEventType.SIGNAL_GENERATED,
                {
                    "symbol": signal.symbol,
                    "signal_type": signal.signal_type.value,
                    "strength": signal.strength.value,
                    "score": round(score, 3),
                    "price": signal.price,
                    "volume_ratio": round(spike.time_normalized_ratio, 1),
                },
            )

        logger.info(
            f"📊 SIGNAL: {signal.signal_type.value} {signal.symbol} | "
            f"score={score:.2f} | strength={signal.strength.value} | "
            f"vol={spike.time_normalized_ratio:.1f}x | ₹{signal.price:.2f}"
        )

        return signal

    def _calculate_score(
        self,
        spike: VolumeScanResult,
        sector_heat: SectorHeat | None,
        delivery_pct: float,
        price_change_pct: float,
    ) -> float:
        """
        Calculate composite signal score (0.0 - 1.0).

        Each factor is normalized to 0.0 - 1.0 then weighted.
        """
        # 1. Volume factor: how strong is the volume spike
        #    2x = 0.3, 3x = 0.5, 5x = 0.8, 10x+ = 1.0
        vol_ratio = spike.time_normalized_ratio
        volume_factor = min(vol_ratio / 10.0, 1.0) * (1.0 + min(vol_ratio / 5.0, 1.0)) / 2.0

        # 2. Sector factor: is the whole sector active
        sector_factor = 0.5  # Default neutral if no sector data
        if sector_heat:
            sector_factor = sector_heat.heat_score

        # 3. Delivery factor: high delivery = institutional interest
        #    0% = 0.0, 30% = 0.3, 50% = 0.5, 80%+ = 1.0
        delivery_factor = min(delivery_pct / 80.0, 1.0)

        # 4. Price momentum factor: is price moving in the volume direction
        #    +5% = 1.0, 0% = 0.5, -5% = 0.0
        momentum_factor = min(max((price_change_pct + 5.0) / 10.0, 0.0), 1.0)

        # 5. Breadth factor: sector advance/decline ratio
        breadth_factor = 0.5
        if sector_heat:
            breadth_factor = min(sector_heat.advance_decline_ratio / 3.0, 1.0)

        # Weighted composite
        score = (
            volume_factor * self.WEIGHT_VOLUME
            + sector_factor * self.WEIGHT_SECTOR
            + delivery_factor * self.WEIGHT_DELIVERY
            + momentum_factor * self.WEIGHT_MOMENTUM
            + breadth_factor * self.WEIGHT_BREADTH
        )

        # ── Bonus scoring from enriched tick data ──

        # Buy/sell pressure: strong buy pressure = institutional demand
        if spike.buy_sell_ratio > 1.5:
            score += 0.03
        elif spike.buy_sell_ratio < 0.67:  # Strong sell pressure
            score -= 0.02

        # Gap-up opening (bullish momentum)
        if spike.gap_pct > 1.0:
            score += 0.02

        # Tight bid-ask spread = high liquidity (good)
        if spike.exchange_vwap > 0 and spike.bid_ask_spread > 0:
            spread_pct = (spike.bid_ask_spread / spike.exchange_vwap) * 100
            if spread_pct < 0.1:  # Very tight spread
                score += 0.02
            elif spread_pct > 0.5:  # Wide spread = penalty
                score -= 0.02

        return min(max(score, 0.0), 1.0)

    def _determine_signal_type(
        self,
        spike: VolumeScanResult,
        price_change_pct: float,
        sector_heat: SectorHeat | None,
    ) -> SignalType:
        """
        Determine if this should be a BUY or SELL signal.

        BUY indicators:
            - Positive price change + volume spike
            - Bullish sector (advance > decline)
            - Strong/Extreme volume strength

        SELL indicators:
            - Negative price change + volume spike (distribution)
            - Bearish sector
        """
        bullish_signals = 0
        bearish_signals = 0

        # Price direction
        if price_change_pct > 0.5:
            bullish_signals += 2
        elif price_change_pct < -0.5:
            bearish_signals += 2

        # Sector direction
        if sector_heat:
            if sector_heat.is_bullish:
                bullish_signals += 1
            else:
                bearish_signals += 1

        # Volume strength context
        if spike.strength in (SignalStrength.STRONG, SignalStrength.EXTREME):
            if price_change_pct > 0:
                bullish_signals += 1  # Strong volume + up = accumulation
            else:
                bearish_signals += 1  # Strong volume + down = distribution

        return SignalType.BUY if bullish_signals >= bearish_signals else SignalType.SELL

    def _get_score_breakdown(
        self,
        spike: VolumeScanResult,
        sector_heat: SectorHeat | None,
        delivery_pct: float,
        price_change_pct: float,
    ) -> dict[str, float]:
        """Get individual factor scores for debugging/transparency."""
        vol_ratio = spike.time_normalized_ratio
        volume_factor = min(vol_ratio / 10.0, 1.0) * (1.0 + min(vol_ratio / 5.0, 1.0)) / 2.0
        sector_factor = sector_heat.heat_score if sector_heat else 0.5
        delivery_factor = min(delivery_pct / 80.0, 1.0)
        momentum_factor = min(max((price_change_pct + 5.0) / 10.0, 0.0), 1.0)
        breadth_factor = min(sector_heat.advance_decline_ratio / 3.0, 1.0) if sector_heat else 0.5

        return {
            "volume": round(volume_factor, 3),
            "sector": round(sector_factor, 3),
            "delivery": round(delivery_factor, 3),
            "momentum": round(momentum_factor, 3),
            "breadth": round(breadth_factor, 3),
        }

    def generate_batch(
        self,
        spikes: list[VolumeScanResult],
        sector_heats: dict[str, SectorHeat] | None = None,
        delivery_data: dict[str, float] | None = None,
        price_changes: dict[str, float] | None = None,
    ) -> list[Signal]:
        """
        Generate signals for a batch of volume spikes.

        Returns:
            List of Signal objects that passed the minimum score threshold.
        """
        signals = []
        sector_heats = sector_heats or {}
        delivery_data = delivery_data or {}
        price_changes = price_changes or {}

        for spike in spikes:
            sector_heat = sector_heats.get(spike.symbol)
            delivery_pct = delivery_data.get(spike.symbol, 0.0)
            price_change = price_changes.get(spike.symbol, 0.0)

            signal = self.generate(spike, sector_heat, delivery_pct, price_change)
            if signal:
                signals.append(signal)

        # Sort by score descending
        signals.sort(key=lambda s: s.score, reverse=True)

        logger.info(
            f"Signal batch: {len(signals)} generated from {len(spikes)} spikes "
            f"(rejected {len(spikes) - len(signals)})"
        )
        return signals

    @property
    def stats(self) -> dict[str, int]:
        return {
            "signals_generated": self._signals_generated,
            "signals_rejected": self._signals_rejected,
        }

    def reset_stats(self) -> None:
        self._signals_generated = 0
        self._signals_rejected = 0
