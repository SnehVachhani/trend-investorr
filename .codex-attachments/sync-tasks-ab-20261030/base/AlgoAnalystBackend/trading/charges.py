"""Configurable Indian equity-delivery transaction charges.

The defaults mirror the current zero-brokerage NSE equity-delivery schedule.
The calculator deliberately keeps each charge separate: backtest results can
show a net P&L while the UI and trade metadata can still explain where the
cost came from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import floor
from typing import Any, Mapping


SEBI_CRORE = 1_00_00_000.0


def _round_paise(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round_rupee(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class DeliveryChargeConfig:
    """Master configuration for one NSE equity-delivery charge schedule."""

    enabled: bool = True
    product_type: str = "DELIVERY"
    exchange: str = "NSE"

    brokerage_rate_pct: float = 0.0
    brokerage_cap_rupees: float = 0.0
    brokerage_min_rupees: float = 0.0
    stt_buy_pct: float = 0.1
    stt_sell_pct: float = 0.1
    transaction_charge_pct: float = 0.0030699
    ipft_charge_pct: float = 0.0000001
    stamp_duty_buy_pct: float = 0.015
    sebi_fee_per_crore: float = 10.0
    dp_charge_per_sell_rupees: float = 12.5
    gst_pct: float = 18.0

    def __post_init__(self) -> None:
        numeric_fields = (
            "brokerage_rate_pct",
            "brokerage_cap_rupees",
            "brokerage_min_rupees",
            "stt_buy_pct",
            "stt_sell_pct",
            "transaction_charge_pct",
            "ipft_charge_pct",
            "stamp_duty_buy_pct",
            "sebi_fee_per_crore",
            "dp_charge_per_sell_rupees",
            "gst_pct",
        )
        if any(float(getattr(self, field)) < 0 for field in numeric_fields):
            raise ValueError("charge rates and amounts cannot be negative")
        if self.brokerage_min_rupees > self.brokerage_cap_rupees:
            raise ValueError("minimum brokerage cannot exceed the brokerage cap")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "DeliveryChargeConfig":
        """Build a config from app_config-style keys, using safe defaults."""
        defaults = cls()
        fields: dict[str, Any] = {}
        for field_name in asdict(defaults):
            raw = values.get(field_name, getattr(defaults, field_name))
            default = getattr(defaults, field_name)
            if isinstance(default, bool):
                if isinstance(raw, str):
                    raw = raw.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    raw = bool(raw)
            elif isinstance(default, float):
                try:
                    raw = float(raw)
                except (TypeError, ValueError):
                    raw = default
            elif isinstance(default, str):
                raw = str(raw)
            fields[field_name] = raw
        return cls(**fields)

    @classmethod
    def from_store(cls, config_store: Any) -> "DeliveryChargeConfig":
        """Load charge keys from an AppConfigStore-like object."""
        defaults = cls()
        values = {
            field_name: config_store.get(
                f"charges_{field_name}",
                str(getattr(defaults, field_name)),
            )
            for field_name in asdict(defaults)
        }
        return cls.from_mapping(values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brokerage_for(self, turnover: float) -> float:
        """Return brokerage for one executed order, before GST."""
        if not self.enabled or turnover <= 0:
            return 0.0
        percentage_fee = turnover * self.brokerage_rate_pct / 100.0
        return _round_paise(min(
            self.brokerage_cap_rupees,
            max(self.brokerage_min_rupees, percentage_fee),
        ))

    def calculate_order(
        self,
        side: str,
        price: float,
        quantity: int,
    ) -> "ChargeBreakdown":
        """Calculate the debit/credit charges for one buy or sell order."""
        normalized_side = side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        turnover = max(0.0, float(price) * max(0, int(quantity)))
        if not self.enabled or turnover == 0:
            return ChargeBreakdown(side=normalized_side, turnover=turnover)

        (
            brokerage,
            stt,
            transaction,
            ipft,
            sebi,
            stamp_duty,
            dp_base,
            gst,
            gst_on_dp,
            total,
        ) = self._rounded_components(normalized_side, turnover)
        return ChargeBreakdown(
            side=normalized_side,
            turnover=turnover,
            brokerage=brokerage,
            stt=stt,
            transaction_charges=transaction,
            ipft=ipft,
            sebi=sebi,
            stamp_duty=stamp_duty,
            dp_charges=dp_base,
            gst=gst,
            gst_on_dp=gst_on_dp,
            total=total,
        )

    def total_for_order(self, side: str, price: float, quantity: int) -> float:
        """Return an order total without allocating a full breakdown.

        The backtest uses only the total while searching for an affordable
        quantity and while marking an open position to market.  This helper
        uses the exact same rounded components and charge order as
        ``calculate_order`` while avoiding a dataclass allocation on those hot
        paths.  Executed trades still use ``calculate_order`` so their full
        explanation remains available to the UI.
        """
        normalized_side = side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        turnover = max(0.0, float(price) * max(0, int(quantity)))
        if not self.enabled or turnover == 0:
            return 0.0
        return self._rounded_components(normalized_side, turnover)[-1]

    def _rounded_components(
        self,
        normalized_side: str,
        turnover: float,
    ) -> tuple[float, float, float, float, float, float, float, float, float, float]:
        """Calculate components in the configured real-world charge order."""
        brokerage = _round_paise(self.brokerage_for(turnover))
        transaction = _round_paise(turnover * self.transaction_charge_pct / 100.0)
        stt_rate = self.stt_buy_pct if normalized_side == "BUY" else self.stt_sell_pct
        stt = _round_rupee(turnover * stt_rate / 100.0)
        ipft = _round_paise(turnover * self.ipft_charge_pct / 100.0)
        sebi = _round_paise(turnover / SEBI_CRORE * self.sebi_fee_per_crore)
        stamp_duty = (
            _round_rupee(turnover * self.stamp_duty_buy_pct / 100.0)
            if normalized_side == "BUY"
            else 0.0
        )
        dp_base = (
            _round_paise(self.dp_charge_per_sell_rupees)
            if normalized_side == "SELL"
            else 0.0
        )
        gst_on_services = (
            brokerage + transaction + ipft + sebi
        ) * self.gst_pct / 100.0
        gst_on_dp = dp_base * self.gst_pct / 100.0
        gst = _round_paise(gst_on_services + gst_on_dp)
        total = _round_paise(
            brokerage
            + transaction
            + stt
            + ipft
            + sebi
            + stamp_duty
            + dp_base
            + gst
        )
        return (
            brokerage,
            stt,
            transaction,
            ipft,
            sebi,
            stamp_duty,
            dp_base,
            gst,
            gst_on_dp,
            total,
        )

    def max_buy_quantity(self, budget: float, price: float) -> int:
        """Find the largest whole-share buy whose gross debit fits the budget."""
        if budget <= 0 or price <= 0:
            return 0
        upper = floor(budget / price)
        low, high = 0, upper
        while low < high:
            candidate = (low + high + 1) // 2
            debit = candidate * price + self.total_for_order("BUY", price, candidate)
            if debit <= budget + 1e-9:
                low = candidate
            else:
                high = candidate - 1
        return low


@dataclass(frozen=True)
class ChargeBreakdown:
    side: str
    turnover: float
    brokerage: float = 0.0
    stt: float = 0.0
    transaction_charges: float = 0.0
    ipft: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    dp_charges: float = 0.0
    gst: float = 0.0
    gst_on_dp: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {key: round(value, 6) if isinstance(value, float) else value for key, value in asdict(self).items()}


def config_from_store(config_store: Any) -> DeliveryChargeConfig:
    """Named helper used by API entry points and spawned workers."""
    return DeliveryChargeConfig.from_store(config_store)
