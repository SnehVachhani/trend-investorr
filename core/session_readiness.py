"""
Trend-Investorr: Trading-session readiness.

Records whether today's lifecycle steps completed, so market open (whether
fired by the scheduler at 09:15 or replayed after a restart) can refuse to
start a half-prepared feed.

Required before the live session may start:
    pre-market  — broker authentication, instrument master loaded
    warm-up     — WebSocket subscription, warm-up data loaded

Non-fatal ("degraded"):
    backfill    — historical candle gaps could not be filled. Scanning and
                  candle collection can still run on their own, but the
                  trading data is unverified: new entries stay blocked and
                  the session is not reported as fully ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionReadiness:
    pre_market_ok: bool = False
    warmup_ok: bool = False
    data_verified: bool = False
    failures: dict[str, str] = field(default_factory=dict)   # required step -> error
    degraded: dict[str, str] = field(default_factory=dict)   # non-fatal step -> note

    def reset(self) -> None:
        """New trading day (or a fresh pre-market run): nothing is ready yet."""
        self.pre_market_ok = False
        self.warmup_ok = False
        self.data_verified = False
        self.failures.clear()
        self.degraded.clear()

    def fail(self, step: str, error: object) -> None:
        self.failures[step] = str(error)

    def degrade(self, step: str, note: object) -> None:
        self.degraded[step] = str(note)

    @property
    def ready_for_open(self) -> bool:
        """May the live feed be started at all?"""
        return self.pre_market_ok and self.warmup_ok

    @property
    def fully_ready(self) -> bool:
        """Ready, and the trading data behind it is verified."""
        return self.ready_for_open and self.data_verified

    def blockers(self) -> str:
        """Human-readable reason(s) the session is not ready for open."""
        parts = []
        if not self.pre_market_ok:
            parts.append("pre-market did not complete")
        if not self.warmup_ok:
            parts.append("scanner warm-up did not complete")
        for step, error in self.failures.items():
            parts.append(f"{step} failed: {error}")
        return "; ".join(parts) or "none"

    def degradations(self) -> str:
        return "; ".join(f"{step}: {note}" for step, note in self.degraded.items()) or "none"
