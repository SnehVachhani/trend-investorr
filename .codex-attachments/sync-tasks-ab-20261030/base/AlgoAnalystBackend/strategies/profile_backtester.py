"""Point-in-time backtesting for saved filter expressions.

The service deliberately knows nothing about PostgreSQL or HTTP.  It evaluates
one immutable rule tree at each completed candle and enters on the next bar's
open, which keeps the profile contract identical to screening and signals.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Any, Iterable

import numpy as np

from data.candle_sessions import INTRADAY_TIMEFRAMES, is_regular_session_candle, normalize_daily_candles
from engine.filter_engine.catalog import FilterCatalog, build_default_catalog
from engine.filter_engine.context import build_fast_backtest_context
from engine.filter_engine.evaluator import FilterEvaluator
from engine.filter_engine.models import (
    CandlePoint,
    EvaluationMode,
    FilterCondition,
    FilterExpression,
    FilterGroup,
    FilterResult,
    FilterStatus,
    GroupOperator,
    Timeframe,
)
from trading.charges import DeliveryChargeConfig


@dataclass(frozen=True)
class ProfileBacktestConfig:
    initial_capital: float = 100_000.0
    position_size_pct: float = 100.0
    # Kept for request compatibility. New runs use the master delivery
    # schedule below; callers should configure charges in the UI instead.
    commission_pct: float | None = None
    charge_config: DeliveryChargeConfig = DeliveryChargeConfig()
    slippage_pct: float = 0.0
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 10.0
    swing_stop_buffer_pct: float = 0.5
    swing_target_r: float = 2.0
    max_hold_bars: int = 10
    # Entry conditions are normally not position-management conditions. Keep
    # this opt-in so a fast 5-minute trigger does not liquidate the position
    # on the very next bar merely because one trigger component reset.
    close_on_filter_fail: bool = False


class _AtomicAllExpressionOutcomes:
    """Fast, semantics-preserving outcomes for cache-backed ALL expressions.

    Curated presets are flat, required ALL expressions composed entirely of
    deterministic OHLCV filters.  Their atomic values have already been
    calculated by ``AtomicBacktestCache``.  Combining those arrays once avoids
    building Pydantic contexts and result trees for every non-entry candle.
    Unsupported or custom expressions intentionally return ``None`` and use
    the original evaluator unchanged.
    """

    def __init__(self, valid: np.ndarray, passed: np.ndarray) -> None:
        self._valid = valid
        self._passed = passed

    @classmethod
    def build(
        cls,
        expression: FilterExpression,
        evaluator: FilterEvaluator,
        primary_timeframe: Timeframe,
        primary_candles: list[CandlePoint],
        lookback_by_timeframe: dict[Timeframe, int],
    ) -> "_AtomicAllExpressionOutcomes | None":
        cache = evaluator.atomic_cache
        if (
            cache is None
            or expression.operator is not GroupOperator.ALL
            or expression.veto
            or expression.minimum_score != 0
            or expression.maximum_insufficient_data != 0
            or not expression.children
            or any(
                not isinstance(child, FilterCondition)
                or not child.required
                or child.weight != 1
                or child.filter_id not in evaluator._CACHEABLE_ATOMIC_FILTERS
                for child in expression.children
            )
        ):
            return None

        count = len(primary_candles)
        if count == 0:
            return None
        primary_timestamps = [candle.timestamp for candle in primary_candles]
        all_valid = np.ones(count, dtype=bool)
        all_passed = np.ones(count, dtype=bool)

        for condition in expression.children:
            context_length = lookback_by_timeframe.get(condition.timeframe)
            if context_length is None:
                return None
            cache_series = cache.get_series(
                evaluator.condition_cache_key(condition),
                context_length,
            )
            if cache_series is None:
                return None

            # Align a slower timeframe to the most recently completed point
            # at every execution candle.  This is the same as the original
            # right-edge walk, performed once rather than per condition/bar.
            values: list[Any] = [None] * count
            source_index = 0
            current_value = None
            source_timestamps = cache_series.timestamps
            for index, timestamp in enumerate(primary_timestamps):
                while (
                    source_index < len(source_timestamps)
                    and source_timestamps[source_index] <= timestamp
                ):
                    current_value = cache_series.values[source_index]
                    source_index += 1
                values[index] = current_value

            available = np.fromiter(
                (value is not None for value in values),
                dtype=bool,
                count=count,
            )
            condition_passed = np.fromiter(
                (bool(value[0]) if value is not None else False for value in values),
                dtype=bool,
                count=count,
            )
            all_valid &= available
            all_passed &= condition_passed

        return cls(all_valid, all_passed)

    def at(self, index: int) -> tuple[bool, FilterStatus]:
        if not bool(self._valid[index]):
            return False, FilterStatus.INSUFFICIENT_DATA
        if bool(self._passed[index]):
            return True, FilterStatus.PASS
        return False, FilterStatus.FAIL


class ProfileBacktester:
    """Run a long-only, next-bar-open backtest for a filter expression."""

    _TIMEFRAME_ORDER = {
        Timeframe.ONE_MINUTE: 0,
        Timeframe.FIVE_MINUTE: 1,
        Timeframe.FIFTEEN_MINUTE: 2,
        Timeframe.ONE_HOUR: 3,
        Timeframe.ONE_DAY: 4,
        Timeframe.ONE_WEEK: 5,
    }

    def __init__(
        self,
        *,
        symbol: str,
        candles: Iterable[CandlePoint | dict[str, Any]] | None = None,
        candles_by_timeframe: dict[Timeframe, Iterable[CandlePoint | dict[str, Any]]] | None = None,
        expression: FilterExpression,
        config: ProfileBacktestConfig | None = None,
        catalog: FilterCatalog | None = None,
        evaluator: FilterEvaluator | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        execution_timeframe: Timeframe | None = None,
    ) -> None:
        self.symbol = symbol
        series = candles_by_timeframe or {
            Timeframe.ONE_DAY: candles or [],
        }
        self.candles_by_timeframe = {
            timeframe: self._normalize(points, timeframe) for timeframe, points in series.items()
        }
        if execution_timeframe is not None and execution_timeframe not in self.candles_by_timeframe:
            raise ValueError(
                f"execution timeframe {execution_timeframe.value} has no loaded candles"
            )
        self.primary_timeframe = execution_timeframe or min(
            self.candles_by_timeframe,
            key=lambda timeframe: self._TIMEFRAME_ORDER.get(timeframe, 99),
        )
        self.candles = self.candles_by_timeframe.get(self.primary_timeframe, [])
        self.expression = expression
        self.config = config or ProfileBacktestConfig()
        self.catalog = catalog or build_default_catalog()
        self.evaluator = evaluator or FilterEvaluator(self.catalog)
        self.start_date = start_date
        self.end_date = end_date

    @staticmethod
    def _normalize(
        candles: Iterable[CandlePoint | dict[str, Any]],
        timeframe: Timeframe | None = None,
    ) -> list[CandlePoint]:
        points = [
            candle if isinstance(candle, CandlePoint) else CandlePoint.model_validate(candle)
            for candle in candles
        ]
        if timeframe == Timeframe.ONE_DAY:
            points, _ = normalize_daily_candles(points)
        elif timeframe is not None and timeframe.value in INTRADAY_TIMEFRAMES:
            points = [point for point in points if is_regular_session_candle(point.timestamp)]
        return sorted(points, key=lambda candle: candle.timestamp)

    @staticmethod
    def _is_before(value: datetime, bound: datetime | None) -> bool:
        if bound is None:
            return False
        try:
            return value < bound
        except TypeError:
            return value.replace(tzinfo=None) < bound.replace(tzinfo=None)

    @staticmethod
    def _is_after(value: datetime, bound: datetime | None) -> bool:
        if bound is None:
            return False
        try:
            return value > bound
        except TypeError:
            return value.replace(tzinfo=None) > bound.replace(tzinfo=None)

    def _lookback_by_timeframe(self) -> dict[Timeframe, int]:
        """Return the bounded point-in-time window required by this expression."""
        lookback: dict[Timeframe, int] = {}

        def visit(node: FilterCondition | FilterGroup) -> None:
            if isinstance(node, FilterGroup):
                for child in node.children:
                    visit(child)
                return
            requirement = self.catalog.requirements_for(
                node.filter_id,
                node.params,
                timeframe=node.timeframe,
            )
            required_candles = requirement.total_candles
            if node.filter_id == "data_completeness":
                required_candles = max(
                    required_candles,
                    int(node.params.get("minimum_candles", 30)),
                )
            lookback[node.timeframe] = max(
                lookback.get(node.timeframe, 0),
                required_candles,
            )

        for node in self.expression.children + self.expression.veto:
            visit(node)
        return lookback

    def run(
        self,
        *,
        _allow_fast_path: bool = True,
        entry_signal_date_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if len(self.candles) < 2:
            raise ValueError("at least two candles are required for next-bar execution")
        if self.config.initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        if not 0 < self.config.position_size_pct <= 100:
            raise ValueError("position size must be between 0 and 100 percent")
        if self.config.stop_loss_pct <= 0:
            raise ValueError("stop loss must be greater than zero")
        if self.config.take_profit_pct < 0:
            raise ValueError("take profit cannot be negative")
        if self.config.max_hold_bars < 0:
            raise ValueError("max hold bars cannot be negative")
        if self.config.take_profit_pct <= 0 and self.config.max_hold_bars <= 0:
            raise ValueError("take profit or max hold bars is required")

        lookback_by_timeframe = self._lookback_by_timeframe()
        timestamps_by_timeframe = {
            timeframe: [point.timestamp for point in points]
            for timeframe, points in self.candles_by_timeframe.items()
        }
        # Validate the immutable rule tree and resolve its requirements once.
        # ``evaluate`` keeps its normal two-argument contract for callers and
        # skips this work after preparation.
        self.evaluator.prepare(self.expression, EvaluationMode.BACKTEST)
        right_edges = {timeframe: 0 for timeframe in self.candles_by_timeframe}
        fast_outcomes = (
            _AtomicAllExpressionOutcomes.build(
                self.expression,
                self.evaluator,
                self.primary_timeframe,
                self.candles,
                lookback_by_timeframe,
            )
            if _allow_fast_path
            else None
        )

        def context_at(candle: CandlePoint, *, advance_edges: bool) -> Any:
            """Build the original point-in-time context only when it is needed."""
            candles_by_timeframe: dict[Timeframe, list[CandlePoint]] = {}
            for timeframe, points in self.candles_by_timeframe.items():
                timestamps = timestamps_by_timeframe[timeframe]
                if advance_edges:
                    right = right_edges[timeframe]
                    while right < len(points) and timestamps[right] <= candle.timestamp:
                        right += 1
                    right_edges[timeframe] = right
                else:
                    right = bisect_right(timestamps, candle.timestamp)
                candles_by_timeframe[timeframe] = points[
                    max(0, right - lookback_by_timeframe.get(timeframe, len(points))):right
                ]
            return build_fast_backtest_context(
                symbol=self.symbol,
                as_of=candle.timestamp,
                candles=candles_by_timeframe,
            )

        cash = float(self.config.initial_capital)
        position: dict[str, Any] | None = None
        trades: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        pending_filter_exit: str | None = None
        pass_count = 0
        fail_count = 0
        insufficient_count = 0
        unavailable_count = 0
        error_count = 0
        last_processed_index = -1
        first_window_timestamp: datetime | None = None
        last_window_timestamp: datetime | None = None
        total_charges = 0.0

        for index, candle in enumerate(self.candles):
            if self._is_after(candle.timestamp, self.end_date):
                break
            last_processed_index = index
            if not self._is_before(candle.timestamp, self.start_date):
                first_window_timestamp = first_window_timestamp or candle.timestamp
                last_window_timestamp = candle.timestamp
            # A filter failure is known only after the current candle closes.
            # When explicitly enabled, exit on the next bar's open rather than
            # pretending that the exact failed close was executable.
            if position is not None and pending_filter_exit is not None:
                trade = self._close_position(
                    position,
                    candle.open * (1 - self.config.slippage_pct / 100),
                    candle.timestamp,
                    pending_filter_exit,
                    cash,
                    exit_index=index,
                )
                cash = trade["cash_after"]
                trades.append(trade)
                position = None
                pending_filter_exit = None

            result: FilterResult | None = None
            if fast_outcomes is not None:
                passed, status = fast_outcomes.at(index)
            else:
                result = self.evaluator.evaluate(
                    self.expression,
                    context_at(candle, advance_edges=True),
                )
                passed, status = result.passed, result.status

            if passed:
                pass_count += 1
            elif status is FilterStatus.INSUFFICIENT_DATA:
                insufficient_count += 1
            elif status is FilterStatus.UNAVAILABLE:
                unavailable_count += 1
            elif status is FilterStatus.ERROR:
                error_count += 1
            else:
                fail_count += 1

            if position is not None:
                exit_price, exit_reason = self._exit_for_bar(position, candle, index)
                if exit_price is not None:
                    trade = self._close_position(position, exit_price, candle.timestamp, exit_reason, cash, exit_index=index)
                    cash = trade["cash_after"]
                    trades.append(trade)
                    position = None

                if (
                    position is not None
                    and self.config.close_on_filter_fail
                    and index > position["entry_index"]
                    and not passed
                ):
                    if index + 1 < len(self.candles):
                        pending_filter_exit = "filter_failed"
                    else:
                        # There is no next tradable bar. The end-of-test close
                        # below is the only honest terminal execution point.
                        pending_filter_exit = "filter_failed"

            # A signal observed on this completed candle is executed at the
            # next candle's open. No current/future candle is used to decide it.
            signal_in_window = not self._is_before(candle.timestamp, self.start_date) and not self._is_after(candle.timestamp, self.end_date)
            signal_session = candle.timestamp.date().isoformat()
            signal_gate_open = entry_signal_date_map is None or signal_session in entry_signal_date_map
            if position is None and signal_in_window and passed and signal_gate_open and index + 1 < len(self.candles):
                next_candle = self.candles[index + 1]
                if self._is_after(next_candle.timestamp, self.end_date):
                    next_candle = None
                if next_candle is not None:
                    # Keep the persisted trade explanation byte-for-byte on
                    # the reference evaluator path.  This is invoked only on
                    # an actual entry signal, not on every rejected candle.
                    entry_result = result or self.evaluator.evaluate(
                        self.expression,
                        context_at(candle, advance_edges=False),
                    )
                    if not entry_result.passed:
                        # A cache defect must never silently create a false
                        # trade. Re-run this one preset from the beginning on
                        # the original evaluator instead of returning a
                        # partial/incorrect result. The fallback leaves every
                        # trade, metric, explanation, and response identical
                        # to the pre-optimization implementation.
                        if fast_outcomes is not None:
                            return self.run(
                                _allow_fast_path=False,
                                entry_signal_date_map=entry_signal_date_map,
                            )
                        raise RuntimeError("reference evaluator changed during backtest")
                    entry_price = next_candle.open * (1 + self.config.slippage_pct / 100)
                    swing_signal = self._passing_swing_profile_signal(entry_result)
                    structural_stop = None
                    structural_target = None
                    if swing_signal is not None and swing_signal.get("swing_low") is not None:
                        structural_stop = float(swing_signal["swing_low"]) * (
                            1 - self.config.swing_stop_buffer_pct / 100
                        )
                        risk = entry_price - structural_stop
                        if 0 < risk <= entry_price * 0.15:
                            structural_target = entry_price + self.config.swing_target_r * risk
                        else:
                            # An opening gap that invalidates the structural
                            # stop must not create a trade with negative risk.
                            structural_stop = None
                    budget = cash * self.config.position_size_pct / 100
                    quantity = (
                        self.config.charge_config.max_buy_quantity(budget, entry_price)
                        if swing_signal is None or structural_target is not None
                        else 0
                    )
                    if quantity > 0:
                        gross_cost = quantity * entry_price
                        entry_charges = self.config.charge_config.calculate_order(
                            "BUY", entry_price, quantity
                        )
                        cash -= gross_cost + entry_charges.total
                        position = {
                            "entry_index": index + 1,
                            "entry_timestamp": next_candle.timestamp,
                            "entry_price": entry_price,
                            "quantity": quantity,
                            "entry_charges": entry_charges,
                            "entry_explanation": entry_result.model_dump(mode="json"),
                            "structural_stop": structural_target is not None,
                            "stop_price": structural_stop,
                            "target_price": structural_target,
                            "target_r": self.config.swing_target_r if structural_target is not None else None,
                            "swing_profile": swing_signal,
                            "scanner_signal_date": (
                                entry_signal_date_map.get(signal_session)
                                if entry_signal_date_map is not None
                                else None
                            ),
                        }
                        # The entry bar is a real holding bar. Check it immediately
                        # so an intrabar stop/target cannot be silently skipped.
                        entry_exit_price, entry_exit_reason = self._exit_for_bar(
                            position,
                            next_candle,
                            index + 1,
                        )
                        if entry_exit_price is not None:
                            trade = self._close_position(
                                position,
                                entry_exit_price,
                                next_candle.timestamp,
                                entry_exit_reason,
                                cash,
                                exit_index=index + 1,
                            )
                            cash = trade["cash_after"]
                            trades.append(trade)
                            position = None

            if not self._is_before(candle.timestamp, self.start_date):
                marked_equity = cash
                if position is not None:
                    estimated_exit = self.config.charge_config.total_for_order(
                        "SELL", candle.close, position["quantity"]
                    )
                    marked_equity += position["quantity"] * candle.close - estimated_exit
                equity_curve.append({"timestamp": candle.timestamp.isoformat(), "equity": marked_equity})

        if first_window_timestamp is None or last_processed_index < 0:
            raise ValueError("no execution candles fall inside the requested date range")

        if position is not None:
            final = self.candles[last_processed_index]
            trade = self._close_position(
                position,
                final.close,
                final.timestamp,
                pending_filter_exit or "end_of_test",
                cash,
                exit_index=len(self.candles) - 1,
            )
            cash = trade["cash_after"]
            trades.append(trade)
            if equity_curve:
                equity_curve[-1]["equity"] = cash

        final_capital = cash
        total_charges = sum(float(trade.get("total_charges", 0.0)) for trade in trades)
        returns = []
        for previous, current in zip(equity_curve, equity_curve[1:]):
            if previous["equity"]:
                returns.append(current["equity"] / previous["equity"] - 1)
        peak = self.config.initial_capital
        max_drawdown = 0.0
        for point in equity_curve:
            peak = max(peak, point["equity"])
            if peak:
                max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak * 100)
        mean_return = sum(returns) / len(returns) if returns else 0.0
        variance = sum((value - mean_return) ** 2 for value in returns) / len(returns) if returns else 0.0
        sharpe = (mean_return / sqrt(variance) * sqrt(len(returns))) if variance > 0 else 0.0
        wins = [trade for trade in trades if trade["pnl"] > 0]
        losses = [trade for trade in trades if trade["pnl"] <= 0]

        return {
            "symbol": self.symbol,
            "execution_timeframe": self.primary_timeframe.value,
            "start_date": first_window_timestamp.isoformat(),
            "end_date": last_window_timestamp.isoformat(),
            "initial_capital": self.config.initial_capital,
            "final_capital": round(final_capital, 6),
            "total_charges": round(total_charges, 6),
            "charge_config": self.config.charge_config.to_dict(),
            "total_return_pct": round((final_capital / self.config.initial_capital - 1) * 100, 6),
            "max_drawdown_pct": round(max_drawdown, 6),
            "sharpe_ratio": round(sharpe, 6),
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 6) if trades else 0.0,
            "signal_counts": {
                "pass": pass_count,
                "fail": fail_count,
                "insufficient_data": insufficient_count,
                "unavailable": unavailable_count,
                "error": error_count,
            },
            "equity_curve": equity_curve,
            "trades": trades,
        }

    def _exit_for_bar(
        self,
        position: dict[str, Any],
        candle: CandlePoint,
        index: int,
    ) -> tuple[float | None, str]:
        stop = position.get("stop_price") or position["entry_price"] * (1 - self.config.stop_loss_pct / 100)
        target = position.get("target_price") or position["entry_price"] * (1 + self.config.take_profit_pct / 100)
        if position.get("structural_stop") and candle.open <= stop:
            return candle.open, "gap_stop"
        if position.get("structural_stop") and target > 0 and candle.open >= target:
            return candle.open, "gap_target"
        if (position.get("structural_stop") or self.config.stop_loss_pct > 0) and candle.low <= stop:
            return stop, "stop_loss"
        if (position.get("structural_stop") or self.config.take_profit_pct > 0) and candle.high >= target:
            return target, "take_profit"
        if self.config.max_hold_bars > 0 and index - position["entry_index"] >= self.config.max_hold_bars:
            return candle.close, "max_hold_bars"
        return None, ""

    def _close_position(
        self,
        position: dict[str, Any],
        exit_price: float,
        exit_timestamp: datetime,
        exit_reason: str,
        cash_before: float,
        exit_index: int | None = None,
    ) -> dict[str, Any]:
        gross_proceeds = position["quantity"] * exit_price
        exit_charges = self.config.charge_config.calculate_order(
            "SELL", exit_price, position["quantity"]
        )
        cash_after = cash_before + gross_proceeds - exit_charges.total
        cost = position["quantity"] * position["entry_price"] + position["entry_charges"].total
        gross_pnl = gross_proceeds - position["quantity"] * position["entry_price"]
        total_charges = position["entry_charges"].total + exit_charges.total
        pnl = gross_pnl - total_charges
        return {
            "symbol": self.symbol,
            "entry_date": position["entry_timestamp"].isoformat(),
            "exit_date": exit_timestamp.isoformat(),
            "entry_price": round(position["entry_price"], 6),
            "exit_price": round(exit_price, 6),
            "quantity": position["quantity"],
            "pnl": round(pnl, 6),
            "pnl_pct": round(pnl / cost * 100, 6) if cost else 0.0,
            "gross_pnl": round(gross_pnl, 6),
            "entry_charges": position["entry_charges"].to_dict(),
            "exit_charges": exit_charges.to_dict(),
            "total_charges": round(total_charges, 6),
            "exit_reason": exit_reason,
            "stop_price": position.get("stop_price"),
            "target_price": position.get("target_price"),
            "target_r": position.get("target_r"),
            "hold_bars": (
                max(0, exit_index - position["entry_index"])
                if exit_index is not None
                else max(0, int((exit_timestamp - position["entry_timestamp"]).days))
            ),
            "entry_explanation": position["entry_explanation"],
            "cash_after": cash_after,
            **(
                {
                    "poc": position["swing_profile"].get("poc"),
                    "swing_low": position["swing_profile"].get("swing_low"),
                    "swing_high": position["swing_profile"].get("swing_high"),
                    "value_area_low": position["swing_profile"].get("value_area_low"),
                    "value_area_high": position["swing_profile"].get("value_area_high"),
                    "stop_buffer_pct": self.config.swing_stop_buffer_pct,
                }
                if position.get("swing_profile")
                else {}
            ),
            **(
                {"scanner_signal_date": position["scanner_signal_date"]}
                if position.get("scanner_signal_date")
                else {}
            ),
        }

    @staticmethod
    def _passing_swing_profile_signal(result: FilterResult) -> dict[str, Any] | None:
        """Find the passing structural condition in a possibly nested expression."""
        if (
            result.filter_id == "bullish_swing_poc_pullback"
            and result.passed
            and isinstance(result.actual_value, dict)
        ):
            return result.actual_value
        for child in result.children:
            found = ProfileBacktester._passing_swing_profile_signal(child)
            if found is not None:
                return found
        return None
