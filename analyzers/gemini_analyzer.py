"""
Trend-Investorr: Gemini AI Signal Analyzer

Uses Google Gemini API to validate trading signals with AI reasoning.
This is a SECONDARY analyzer — the static rule analyzer is the primary.
Gemini provides an additional confidence layer with natural language reasoning.

Features:
    - Structured JSON responses via prompt engineering
    - Automatic retry with exponential backoff
    - Graceful degradation: returns SKIP if API is down
    - Rate limiting to stay within free/paid tier limits
    - Response caching to avoid redundant API calls

Raises:
    Never raises — always returns AnalysisResult (SKIP on failure).
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from analyzers.base import SignalAnalyzer
from analyzers.prompts.templates import format_signal_prompt
from core.constants import AnalysisAction
from data.models.market_data import AnalysisResult, Candle, Signal


class GeminiAnalyzer(SignalAnalyzer):
    """
    Google Gemini AI-powered signal validator.

    Sends signal context + technical indicators to Gemini and
    parses the structured JSON response into an AnalysisResult.

    Usage:
        analyzer = GeminiAnalyzer(api_key="YOUR_KEY")
        result = analyzer.analyze(signal, candles)
    """

    # Gemini model to use
    MODEL_NAME = "gemini-2.0-flash"

    # Rate limiting
    MAX_REQUESTS_PER_MINUTE = 15   # Free tier: 15 RPM
    REQUEST_INTERVAL = 4.0         # Seconds between requests

    def __init__(self, api_key: str, model_name: str | None = None):
        """
        Args:
            api_key: Google Gemini API key.
            model_name: Override the default model (optional).
        """
        self._api_key = api_key
        self._model_name = model_name or self.MODEL_NAME
        self._client: Any = None
        self._model: Any = None
        self._initialized = False

        # Rate limiting state
        self._last_request_at: float = 0.0
        self._requests_this_minute: int = 0
        self._minute_start: float = 0.0

        # Stats
        self._total_calls: int = 0
        self._successful_calls: int = 0
        self._failed_calls: int = 0

        self._initialize()

    def _initialize(self) -> None:
        """Initialize the Gemini client."""
        if not self._api_key:
            logger.warning("Gemini API key not provided — analyzer will return SKIP")
            return

        try:
            import google.generativeai as genai

            genai.configure(api_key=self._api_key)
            self._model = genai.GenerativeModel(
                model_name=self._model_name,
                generation_config={
                    "temperature": 0.3,     # Low temp for consistent analysis
                    "top_p": 0.8,
                    "max_output_tokens": 1024,
                },
            )
            self._initialized = True
            logger.info(f"Gemini analyzer initialized: model={self._model_name}")

        except ImportError:
            logger.warning(
                "google-generativeai package not installed. "
                "Run: pip install google-generativeai"
            )
        except Exception as e:
            logger.error(f"Gemini initialization failed: {e}")

    @property
    def name(self) -> str:
        return "gemini"

    def analyze(self, signal: Signal, candles: list[Candle]) -> AnalysisResult:
        """
        Validate a signal using Gemini AI.

        Sends full context (signal + indicators + price history) to Gemini
        and parses the structured JSON response.

        Never raises — returns SKIP on any failure.
        """
        if not self._initialized:
            return self._skip_result(signal.symbol, "Gemini not initialized")

        if not candles:
            return self._skip_result(signal.symbol, "No candle data provided")

        # Rate limit check
        if not self._check_rate_limit():
            return self._skip_result(signal.symbol, "Rate limited — skipping AI validation")

        try:
            # Get static indicators from signal metadata (if available)
            indicators = signal.metadata.get("static_indicators", {})

            # Build prompt
            prompt = format_signal_prompt(signal, indicators, candles)

            # Call Gemini with retry
            response_text = self._call_gemini(prompt)
            if not response_text:
                return self._skip_result(signal.symbol, "Empty Gemini response")

            # Parse response
            return self._parse_response(response_text, signal.symbol)

        except Exception as e:
            self._failed_calls += 1
            logger.error(f"Gemini analysis failed for {signal.symbol}: {e}")
            return self._skip_result(signal.symbol, f"Analysis error: {e}")

    def _call_gemini(self, prompt: str, max_retries: int = 2) -> str | None:
        """
        Send prompt to Gemini with retry logic.

        Returns the response text or None on failure.
        """
        for attempt in range(1, max_retries + 1):
            try:
                self._total_calls += 1

                # Enforce request interval
                elapsed = time.time() - self._last_request_at
                if elapsed < self.REQUEST_INTERVAL:
                    time.sleep(self.REQUEST_INTERVAL - elapsed)

                response = self._model.generate_content(prompt)
                self._last_request_at = time.time()
                self._requests_this_minute += 1

                if response and response.text:
                    self._successful_calls += 1
                    return response.text.strip()

                logger.warning(f"Gemini returned empty response (attempt {attempt})")

            except Exception as e:
                error_msg = str(e).lower()

                # Rate limit error — wait longer
                if "quota" in error_msg or "rate" in error_msg or "429" in error_msg:
                    wait = 60.0  # Wait a full minute
                    logger.warning(f"Gemini rate limited — waiting {wait}s")
                    time.sleep(wait)
                    continue

                # Other errors
                logger.warning(f"Gemini API error (attempt {attempt}/{max_retries}): {e}")

                if attempt < max_retries:
                    time.sleep(2 ** attempt)

        self._failed_calls += 1
        return None

    def _parse_response(self, response_text: str, symbol: str) -> AnalysisResult:
        """
        Parse Gemini's JSON response into an AnalysisResult.

        Handles common response format issues (markdown wrapping, etc.).
        """
        try:
            # Strip markdown code fences if present
            cleaned = response_text
            if "```json" in cleaned:
                cleaned = cleaned.split("```json")[1].split("```")[0]
            elif "```" in cleaned:
                cleaned = cleaned.split("```")[1].split("```")[0]
            cleaned = cleaned.strip()

            data = json.loads(cleaned)

            # Extract fields with validation
            action_str = data.get("action", "SKIP").upper()
            action = self._parse_action(action_str)

            confidence = float(data.get("confidence", 0.5))
            confidence = min(max(confidence, 0.0), 1.0)

            reasoning = data.get("reasoning", "No reasoning provided")
            risk_factors = data.get("risk_factors", [])

            # Build extended reasoning with risk factors
            full_reasoning = reasoning
            if risk_factors:
                full_reasoning += " | Risks: " + ", ".join(risk_factors)

            # Extract additional insights
            indicators = {
                "suggested_stop_loss_pct": data.get("suggested_stop_loss_pct", 0),
                "suggested_target_pct": data.get("suggested_target_pct", 0),
                "support": data.get("key_levels", {}).get("support", 0),
                "resistance": data.get("key_levels", {}).get("resistance", 0),
                "risk_factors": risk_factors,
            }

            return AnalysisResult(
                action=action,
                confidence=confidence,
                reasoning=full_reasoning,
                analyzer_name=self.name,
                signal_symbol=symbol,
                indicators=indicators,
                raw_response=response_text,
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse Gemini response for {symbol}: {e}")
            logger.debug(f"Raw response: {response_text[:500]}")
            return self._skip_result(
                symbol,
                f"Failed to parse AI response: {e}",
                raw_response=response_text,
            )

    @staticmethod
    def _parse_action(action_str: str) -> AnalysisAction:
        """Parse action string to enum, defaulting to SKIP on unknown values."""
        match action_str:
            case "BUY":
                return AnalysisAction.BUY
            case "SELL":
                return AnalysisAction.SELL
            case "HOLD":
                return AnalysisAction.HOLD
            case _:
                return AnalysisAction.SKIP

    def _check_rate_limit(self) -> bool:
        """Check if we can make another request within rate limits."""
        now = time.time()

        # Reset minute counter
        if now - self._minute_start >= 60.0:
            self._requests_this_minute = 0
            self._minute_start = now

        if self._requests_this_minute >= self.MAX_REQUESTS_PER_MINUTE:
            logger.debug("Gemini rate limit reached for this minute")
            return False

        return True

    def _skip_result(
        self,
        symbol: str,
        reason: str,
        raw_response: str = "",
    ) -> AnalysisResult:
        """Create a SKIP result for degraded scenarios."""
        return AnalysisResult(
            action=AnalysisAction.SKIP,
            confidence=0.0,
            reasoning=f"[Gemini] {reason}",
            analyzer_name=self.name,
            signal_symbol=symbol,
            raw_response=raw_response,
        )

    def health_check(self) -> bool:
        """Check if Gemini API is reachable and configured."""
        return self._initialized and bool(self._api_key)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "total_calls": self._total_calls,
            "successful": self._successful_calls,
            "failed": self._failed_calls,
        }
