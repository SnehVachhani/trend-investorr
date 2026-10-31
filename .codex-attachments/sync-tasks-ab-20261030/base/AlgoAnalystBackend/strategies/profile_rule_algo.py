"""TradingAlgorithm adapter for a published modular filter profile."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.constants import SignalSource
from data.models.market_data import Observation, Tick
from engine.filter_engine.evaluator import FilterEvaluator
from engine.filter_engine.models import EvaluationContext, FilterExpression, FilterStatus
from strategies.base import EntrySignal, ExitSignal, TradingAlgorithm


class ProfileRuleAlgorithm(TradingAlgorithm):
    """Turn scanner observations into entry proposals only when a profile passes."""

    def __init__(
        self,
        *,
        profile_id: int,
        profile_version: int,
        expression: FilterExpression,
        context_provider: Callable[[Observation], EvaluationContext],
        interested_sources: list[SignalSource] | None = None,
        stop_loss_pct: float = 1.0,
        target_pct: float = 2.0,
        enabled: bool = True,
        evaluator: FilterEvaluator | None = None,
    ):
        super().__init__(name=f"profile:{profile_id}:{profile_version}", enabled=enabled)
        self.profile_id = profile_id
        self.profile_version = profile_version
        self.expression = expression
        self._context_provider = context_provider
        self._interested_sources = interested_sources or list(SignalSource)
        self._stop_loss_pct = stop_loss_pct
        self._target_pct = target_pct
        self._evaluator = evaluator or FilterEvaluator()
        self._observations: dict[str, Observation] = {}
        self.last_evaluations: dict[str, dict[str, Any]] = {}

    def interested_sources(self) -> list[SignalSource]:
        return self._interested_sources

    def on_observation(self, obs: Observation) -> None:
        if self.enabled:
            self._observations[obs.symbol] = obs

    def on_tick(self, tick: Tick) -> None:
        trade = self.active_trades.get(tick.symbol)
        if trade:
            trade.update_price(tick.ltp)

    def check_entries(self) -> list[EntrySignal]:
        if not self.enabled:
            return []
        entries: list[EntrySignal] = []
        for symbol, observation in list(self._observations.items()):
            if self.is_in_trade(symbol):
                continue
            context = self._context_provider(observation)
            result = self._evaluator.evaluate(self.expression, context)
            serialized = result.model_dump(mode="json")
            self.last_evaluations[symbol] = serialized
            if result.status is not FilterStatus.PASS or not result.passed:
                continue
            is_buy = observation.direction.lower() in {"bullish", "buy", "long"}
            direction = "BUY" if is_buy else "SELL"
            multiplier = 1 if is_buy else -1
            stop_loss = observation.price * (1 - multiplier * self._stop_loss_pct / 100)
            target_price = observation.price * (1 + multiplier * self._target_pct / 100)
            entries.append(
                EntrySignal(
                    symbol=observation.symbol,
                    token=observation.token,
                    algorithm_name=self.name,
                    direction=direction,
                    price=observation.price,
                    stop_loss=stop_loss,
                    target_price=target_price,
                    confidence=result.score,
                    reason=result.reason or "filter profile passed",
                    triggering_observations=[observation],
                    metadata={
                        "profile_id": self.profile_id,
                        "profile_version": self.profile_version,
                        "evaluation": serialized,
                        "data_as_of": context.as_of.isoformat(),
                    },
                )
            )
        return entries

    def check_exits(self) -> list[ExitSignal]:
        # Existing order/risk management continues to own stops and exits.
        return []
