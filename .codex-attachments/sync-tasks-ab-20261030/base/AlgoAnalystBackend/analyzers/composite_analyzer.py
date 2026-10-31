"""
Trend-Investorr: Composite Analyzer

Chains multiple analyzers and combines their results using a
configurable voting/merging strategy.

Strategies:
    UNANIMOUS   — All analyzers must agree on action (most conservative)
    MAJORITY    — Action with most votes wins
    WEIGHTED    — Confidence-weighted average (default)
    PRIMARY     — Use first analyzer's result, enrich with others' insights

Default: WEIGHTED with StaticRuleAnalyzer (70%) + GeminiAnalyzer (30%)
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from loguru import logger

from analyzers.base import SignalAnalyzer
from core.constants import AnalysisAction, CompositeStrategy
from data.models.market_data import AnalysisResult, Candle, Signal


class CompositeAnalyzer(SignalAnalyzer):
    """
    Combines multiple SignalAnalyzer implementations.

    Usage:
        composite = CompositeAnalyzer(
            analyzers=[static_analyzer, gemini_analyzer],
            weights=[0.7, 0.3],
            strategy=CompositeStrategy.WEIGHTED,
        )
        result = composite.analyze(signal, candles)
    """

    def __init__(
        self,
        analyzers: list[SignalAnalyzer],
        weights: list[float] | None = None,
        strategy: CompositeStrategy = CompositeStrategy.WEIGHTED,
    ):
        """
        Args:
            analyzers: List of SignalAnalyzer implementations.
            weights: Confidence weights for each analyzer (must sum to ~1.0).
                     If None, equal weights are assigned.
            strategy: Voting/merging strategy.
        """
        if not analyzers:
            raise ValueError("CompositeAnalyzer requires at least one analyzer")

        self._analyzers = analyzers
        self._strategy = strategy

        # Default to equal weights
        if weights is None:
            n = len(analyzers)
            self._weights = [1.0 / n] * n
        else:
            if len(weights) != len(analyzers):
                raise ValueError(
                    f"Weights length ({len(weights)}) must match "
                    f"analyzers length ({len(analyzers)})"
                )
            self._weights = weights

        logger.info(
            f"CompositeAnalyzer initialized: strategy={strategy.value}, "
            f"analyzers={[a.name for a in analyzers]}, "
            f"weights={[f'{w:.0%}' for w in self._weights]}"
        )

    @property
    def name(self) -> str:
        return "composite"

    def analyze(self, signal: Signal, candles: list[Candle]) -> AnalysisResult:
        """
        Run all analyzers and combine results.

        Analyzers that return SKIP are excluded from voting
        (their weight is redistributed to healthy analyzers).
        """
        try:
            # Collect results from all analyzers
            results: list[tuple[AnalysisResult, float]] = []

            for analyzer, weight in zip(self._analyzers, self._weights):
                try:
                    if not analyzer.health_check():
                        logger.debug(f"Analyzer '{analyzer.name}' unhealthy — skipping")
                        continue

                    result = analyzer.analyze(signal, candles)

                    # Store static indicators for Gemini to use
                    if analyzer.name == "static" and result.indicators:
                        signal.metadata["static_indicators"] = result.indicators

                    results.append((result, weight))

                except Exception as e:
                    logger.error(f"Analyzer '{analyzer.name}' crashed: {e}")
                    continue

            if not results:
                return AnalysisResult(
                    action=AnalysisAction.SKIP,
                    confidence=0.0,
                    reasoning="All analyzers failed or returned unhealthy",
                    analyzer_name=self.name,
                    signal_symbol=signal.symbol,
                )

            # Filter out SKIP results for voting (but keep their insights)
            actionable = [(r, w) for r, w in results if r.action != AnalysisAction.SKIP]
            skip_results = [(r, w) for r, w in results if r.action == AnalysisAction.SKIP]

            if not actionable:
                # All analyzers returned SKIP
                combined_reasoning = " | ".join(
                    f"[{r.analyzer_name}] {r.reasoning}" for r, _ in results
                )
                return AnalysisResult(
                    action=AnalysisAction.SKIP,
                    confidence=0.0,
                    reasoning=combined_reasoning,
                    analyzer_name=self.name,
                    signal_symbol=signal.symbol,
                )

            # Apply strategy
            match self._strategy:
                case CompositeStrategy.UNANIMOUS:
                    return self._unanimous(actionable, signal.symbol)
                case CompositeStrategy.MAJORITY:
                    return self._majority(actionable, signal.symbol)
                case CompositeStrategy.WEIGHTED:
                    return self._weighted(actionable, signal.symbol)
                case CompositeStrategy.PRIMARY:
                    return self._primary(results, signal.symbol)
                case _:
                    return self._weighted(actionable, signal.symbol)

        except Exception as e:
            logger.error(f"Composite analyzer error: {e}")
            return AnalysisResult(
                action=AnalysisAction.SKIP,
                confidence=0.0,
                reasoning=f"Composite analysis error: {e}",
                analyzer_name=self.name,
                signal_symbol=signal.symbol,
            )

    def _unanimous(
        self,
        results: list[tuple[AnalysisResult, float]],
        symbol: str,
    ) -> AnalysisResult:
        """All analyzers must agree on the action."""
        actions = {r.action for r, _ in results}

        if len(actions) == 1:
            # All agree
            action = actions.pop()
            avg_confidence = sum(r.confidence * w for r, w in results) / sum(w for _, w in results)
            reasoning = " | ".join(f"[{r.analyzer_name}] {r.reasoning}" for r, _ in results)

            return AnalysisResult(
                action=action,
                confidence=avg_confidence,
                reasoning=f"UNANIMOUS {action.value}: {reasoning}",
                analyzer_name=self.name,
                signal_symbol=symbol,
                indicators=self._merge_indicators(results),
            )
        else:
            # Disagreement → SKIP
            reasoning = " | ".join(
                f"[{r.analyzer_name}] {r.action.value} ({r.confidence:.0%})" for r, _ in results
            )
            return AnalysisResult(
                action=AnalysisAction.SKIP,
                confidence=0.0,
                reasoning=f"No consensus — {reasoning}",
                analyzer_name=self.name,
                signal_symbol=symbol,
            )

    def _majority(
        self,
        results: list[tuple[AnalysisResult, float]],
        symbol: str,
    ) -> AnalysisResult:
        """Action with the most votes wins."""
        vote_counts: Counter[AnalysisAction] = Counter()
        for r, _ in results:
            vote_counts[r.action] += 1

        winning_action, count = vote_counts.most_common(1)[0]

        # Average confidence of winning voters
        winners = [(r, w) for r, w in results if r.action == winning_action]
        avg_confidence = sum(r.confidence * w for r, w in winners) / sum(w for _, w in winners)

        reasoning = " | ".join(f"[{r.analyzer_name}] {r.reasoning}" for r, _ in results)

        return AnalysisResult(
            action=winning_action,
            confidence=avg_confidence,
            reasoning=f"MAJORITY ({count}/{len(results)}): {reasoning}",
            analyzer_name=self.name,
            signal_symbol=symbol,
            indicators=self._merge_indicators(results),
        )

    def _weighted(
        self,
        results: list[tuple[AnalysisResult, float]],
        symbol: str,
    ) -> AnalysisResult:
        """Confidence-weighted decision."""
        # Redistribute weights for active analyzers
        total_weight = sum(w for _, w in results)
        if total_weight == 0:
            total_weight = 1.0

        # Score each action by weighted confidence
        action_scores: dict[AnalysisAction, float] = {}
        for result, weight in results:
            normalized_weight = weight / total_weight
            score = result.confidence * normalized_weight
            action_scores[result.action] = action_scores.get(result.action, 0) + score

        # Winner = highest weighted score
        winning_action = max(action_scores, key=action_scores.get)  # type: ignore
        weighted_confidence = sum(
            r.confidence * (w / total_weight)
            for r, w in results
            if r.action == winning_action
        )

        reasoning = " | ".join(
            f"[{r.analyzer_name}] {r.action.value} ({r.confidence:.0%}, w={w / total_weight:.0%})"
            for r, w in results
        )

        return AnalysisResult(
            action=winning_action,
            confidence=min(weighted_confidence, 1.0),
            reasoning=f"WEIGHTED: {reasoning}",
            analyzer_name=self.name,
            signal_symbol=symbol,
            indicators=self._merge_indicators(results),
        )

    def _primary(
        self,
        results: list[tuple[AnalysisResult, float]],
        symbol: str,
    ) -> AnalysisResult:
        """Use first analyzer's decision, enriched with secondary insights."""
        primary_result, _ = results[0]

        # Collect secondary insights
        secondary_notes = []
        merged_indicators = dict(primary_result.indicators)

        for result, _ in results[1:]:
            secondary_notes.append(f"[{result.analyzer_name}] {result.action.value} ({result.confidence:.0%})")
            # Merge unique indicator keys
            for k, v in result.indicators.items():
                if k not in merged_indicators:
                    merged_indicators[f"{result.analyzer_name}_{k}"] = v

        enriched_reasoning = primary_result.reasoning
        if secondary_notes:
            enriched_reasoning += " | Secondary: " + ", ".join(secondary_notes)

        return AnalysisResult(
            action=primary_result.action,
            confidence=primary_result.confidence,
            reasoning=enriched_reasoning,
            analyzer_name=self.name,
            signal_symbol=symbol,
            indicators=merged_indicators,
        )

    @staticmethod
    def _merge_indicators(
        results: list[tuple[AnalysisResult, float]],
    ) -> dict[str, Any]:
        """Merge indicator dicts from all analyzers."""
        merged: dict[str, Any] = {}
        for result, _ in results:
            for k, v in result.indicators.items():
                key = f"{result.analyzer_name}_{k}" if k in merged else k
                merged[key] = v
        return merged

    def health_check(self) -> bool:
        """Healthy if at least one analyzer is healthy."""
        return any(a.health_check() for a in self._analyzers)
