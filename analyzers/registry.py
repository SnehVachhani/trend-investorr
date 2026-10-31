"""
Trend-Investorr: Analyzer Registry

Factory + registry for creating analyzer instances based on configuration.
Supports runtime switching between analyzer strategies via app_config.

Default configuration:
    - "static" → StaticRuleAnalyzer only (no API key needed)
    - "gemini" → GeminiAnalyzer only (needs GEMINI_API_KEY)
    - "composite" → StaticRuleAnalyzer (70%) + GeminiAnalyzer (30%)
    - "composite_conservative" → Both with UNANIMOUS strategy

Usage:
    registry = AnalyzerRegistry(vault=vault)
    analyzer = registry.create("composite")
    result = analyzer.analyze(signal, candles)

    # Runtime switch
    analyzer = registry.create("static")  # No AI overhead
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from analyzers.base import SignalAnalyzer
from analyzers.static_rule_analyzer import StaticRuleAnalyzer
from analyzers.gemini_analyzer import GeminiAnalyzer
from analyzers.composite_analyzer import CompositeAnalyzer
from core.constants import CompositeStrategy
from core.exceptions import ConfigurationError
from core.security.vault import SecureVault


class AnalyzerRegistry:
    """
    Factory for creating analyzer instances.

    Reads credentials from the vault and creates the appropriate
    analyzer based on the configuration string.
    """

    # Known analyzer configurations
    KNOWN_CONFIGS = {
        "static",
        "gemini",
        "composite",
        "composite_conservative",
        "composite_ai_primary",
    }

    def __init__(self, vault: SecureVault | None = None):
        """
        Args:
            vault: SecureVault for reading API keys.
                   Required for Gemini-based analyzers.
        """
        self._vault = vault
        self._instances: dict[str, SignalAnalyzer] = {}

    def create(self, config_name: str = "static") -> SignalAnalyzer:
        """
        Create an analyzer instance by configuration name.

        Caches instances — calling create("static") twice returns the same object.

        Args:
            config_name: One of the KNOWN_CONFIGS names.

        Returns:
            A SignalAnalyzer implementation.

        Raises:
            ConfigurationError: If the config name is unknown.
        """
        # Return cached instance
        if config_name in self._instances:
            return self._instances[config_name]

        # Build new instance
        match config_name:
            case "static":
                analyzer = self._create_static()

            case "gemini":
                analyzer = self._create_gemini()

            case "composite":
                analyzer = self._create_composite(
                    weights=[0.7, 0.3],
                    strategy=CompositeStrategy.WEIGHTED,
                )

            case "composite_conservative":
                analyzer = self._create_composite(
                    weights=[0.5, 0.5],
                    strategy=CompositeStrategy.UNANIMOUS,
                )

            case "composite_ai_primary":
                analyzer = self._create_composite(
                    weights=[0.3, 0.7],
                    strategy=CompositeStrategy.WEIGHTED,
                )

            case _:
                raise ConfigurationError(
                    f"Unknown analyzer config: '{config_name}'. "
                    f"Known configs: {self.KNOWN_CONFIGS}",
                    details={"requested": config_name, "available": list(self.KNOWN_CONFIGS)},
                )

        self._instances[config_name] = analyzer
        logger.info(f"Analyzer created: '{config_name}' → {analyzer.name}")
        return analyzer

    def _create_static(self) -> StaticRuleAnalyzer:
        """Create a StaticRuleAnalyzer (no dependencies)."""
        return StaticRuleAnalyzer()

    def _create_gemini(self) -> GeminiAnalyzer:
        """Create a GeminiAnalyzer (needs API key from vault)."""
        api_key = self._get_gemini_key()
        return GeminiAnalyzer(api_key=api_key)

    def _create_composite(
        self,
        weights: list[float],
        strategy: CompositeStrategy,
    ) -> CompositeAnalyzer:
        """
        Create a CompositeAnalyzer with static + gemini.

        If Gemini key is not available, falls back to static-only.
        """
        static = self._create_static()
        analyzers: list[SignalAnalyzer] = [static]
        actual_weights = [1.0]  # Default to static-only

        try:
            gemini_key = self._get_gemini_key()
            if gemini_key:
                gemini = GeminiAnalyzer(api_key=gemini_key)
                if gemini.health_check():
                    analyzers.append(gemini)
                    actual_weights = weights
                else:
                    logger.warning("Gemini unhealthy — composite using static-only")
            else:
                logger.warning("No Gemini API key — composite using static-only")
        except Exception as e:
            logger.warning(f"Gemini unavailable: {e} — composite using static-only")

        return CompositeAnalyzer(
            analyzers=analyzers,
            weights=actual_weights,
            strategy=strategy,
        )

    def _get_gemini_key(self) -> str:
        """Get Gemini API key from vault."""
        if not self._vault:
            return ""

        return self._vault.get_credential("GEMINI_API_KEY") or ""

    def list_available(self) -> list[dict[str, Any]]:
        """List all available analyzer configurations with their status."""
        has_gemini = bool(self._get_gemini_key()) if self._vault else False

        return [
            {
                "name": "static",
                "description": "Rule-based: RSI, EMA, VWAP, delivery %",
                "requires_api_key": False,
                "available": True,
            },
            {
                "name": "gemini",
                "description": "Google Gemini AI validation",
                "requires_api_key": True,
                "available": has_gemini,
            },
            {
                "name": "composite",
                "description": "Static (70%) + Gemini (30%) weighted",
                "requires_api_key": False,  # Degrades to static-only
                "available": True,
            },
            {
                "name": "composite_conservative",
                "description": "Static + Gemini unanimous (most conservative)",
                "requires_api_key": False,
                "available": True,
            },
            {
                "name": "composite_ai_primary",
                "description": "Gemini (70%) + Static (30%) — AI-heavy",
                "requires_api_key": False,
                "available": True,
            },
        ]

    def clear_cache(self) -> None:
        """Clear cached analyzer instances (for config reload)."""
        self._instances.clear()
        logger.debug("Analyzer registry cache cleared")
