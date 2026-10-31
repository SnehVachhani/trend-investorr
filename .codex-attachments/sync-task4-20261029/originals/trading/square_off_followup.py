"""
Square-off follow-up: did the broker position actually become flat?

At 15:15 every live exit is only PLACED — the broker acknowledged it, no
fill is known. This module checks afterwards, read-only:

    SquareOffConfirmation   what the broker's books show now
    SquareOffFollowUp       runs OrderManager.confirm_square_off() a few
                            times in a background thread after square-off
                            (never blocking the scheduler), then sends ONE
                            alert: "broker confirmed flat", or exactly what
                            is still open / working / rejected / unreadable.

It never places, cancels or retries an order. A position is reported flat
only when the broker's position book shows no open intraday quantity.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Callable

from loguru import logger

from core.constants import IST

# No square-off activity at or after market close; the runner stops here.
FOLLOWUP_DEADLINE = time(15, 29, 30)


@dataclass(frozen=True)
class OpenPositionStatus:
    """One broker INTRADAY row with open quantity, and the exit working for it (if any)."""

    trading_symbol: str
    token: str
    exchange: str
    net_qty: int
    exit_order_id: str = ""     # the most relevant same-side exit order today, if any
    exit_status: str = ""       # its broker status ("open", "rejected", ...); "" = none found
    exit_qty: int = 0
    filled: int = 0
    unfilled: int = 0
    detail: str = ""            # e.g. "order book unreadable — exit status unknown"

    def describe(self) -> str:
        head = f"{self.trading_symbol}: {abs(self.net_qty)} still {'long' if self.net_qty > 0 else 'short'}"
        if self.detail:
            return f"{head} — {self.detail}"
        if not self.exit_order_id:
            return f"{head} — no exit working at the broker"
        if self.exit_status in ("complete", "completed", "filled"):
            return (
                f"{head} — exit {self.exit_order_id} reports complete ({self.filled}/{self.exit_qty}) "
                f"but the position book still shows open quantity"
            )
        if self.exit_status in ("rejected", "cancelled", "canceled"):
            return f"{head} — last exit {self.exit_order_id} {self.exit_status}; no exit working"
        return (
            f"{head} — exit {self.exit_order_id} {self.exit_status}, {self.filled}/{self.exit_qty} "
            f"filled, {self.unfilled} open"
        )


@dataclass
class SquareOffConfirmation:
    checked_at: datetime
    # True: broker position book shows no open intraday quantity (every row
    # readable). False: open quantity remains. None: books could not be read.
    flat: bool | None = None
    error: str | None = None
    open_positions: list[OpenPositionStatus] = field(default_factory=list)
    rows_unreadable: int = 0

    @property
    def confirmed_flat(self) -> bool:
        return self.flat is True

    def describe(self) -> str:
        when = f"{self.checked_at:%H:%M:%S}"
        if self.flat is True:
            return f"Broker intraday book confirmed flat at {when}."
        lines: list[str] = []
        if self.error:
            lines.append(f"{self.error}")
        for pos in self.open_positions:
            lines.append(pos.describe())
        if self.rows_unreadable:
            lines.append(f"{self.rows_unreadable} broker position row(s) unreadable")
        if self.flat is None:
            lines.insert(0, f"At {when} the broker's books could NOT be read — flat NOT confirmed.")
        else:
            lines.insert(0, f"At {when} the broker still shows open intraday quantity — NOT flat.")
        return "\n".join(lines)


class SquareOffFollowUp:
    """
    After square-off: wait, check, repeat a few times, then alert once.

    Runs in a daemon thread (start()) so the scheduler job that triggered
    square-off returns immediately. run() is synchronous for tests and for
    callers that already are in the background.
    """

    def __init__(
        self,
        order_manager: Any,
        notifier: Any,
        *,
        delay_s: float = 60,
        interval_s: float = 60,
        max_attempts: int = 5,
        deadline: time = FOLLOWUP_DEADLINE,
        now: Callable[[], datetime] | None = None,
        wait: Callable[[float], None] | None = None,
    ):
        self._order_manager = order_manager
        self._notifier = notifier
        self._delay_s = delay_s
        self._interval_s = interval_s
        self._max_attempts = max(1, max_attempts)
        self._deadline = deadline
        self._now = now or (lambda: datetime.now(IST))
        # threading.Event.wait is used rather than time.sleep so a patched
        # time module (tests, fake clocks) never affects it.
        self._wait = wait or (lambda seconds: threading.Event().wait(seconds))

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run_guarded, name="squareoff-followup", daemon=True)
        thread.start()
        return thread

    def _run_guarded(self) -> None:
        try:
            self.run()
        except Exception as e:  # pragma: no cover - last-resort reporting
            logger.exception(f"Square-off follow-up crashed: {e}")
            try:
                self._notifier.risk_alert(
                    "Square Off — follow-up check FAILED",
                    f"The post-square-off flat check crashed ({e}); flat NOT confirmed. "
                    f"Check the broker's positions manually.",
                )
            except Exception:
                pass

    def run(self) -> SquareOffConfirmation:
        outcome: SquareOffConfirmation | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._wait(self._delay_s if attempt == 1 else self._interval_s)
            outcome = self._order_manager.confirm_square_off()
            if outcome.confirmed_flat:
                break
            logger.warning(
                f"Square-off follow-up {attempt}/{self._max_attempts}: flat NOT confirmed — "
                + outcome.describe().replace("\n", " | ")
            )
            if self._now().time() >= self._deadline:
                logger.warning("Square-off follow-up: at/after the close deadline — stopping checks")
                break
        assert outcome is not None
        self._alert(outcome)
        return outcome

    def _alert(self, outcome: SquareOffConfirmation) -> None:
        if outcome.confirmed_flat:
            logger.info(f"Square-off follow-up: {outcome.describe()}")
            self._notifier.system_alert("Square Off — broker confirmed flat", outcome.describe())
            return
        details = (
            outcome.describe()
            + "\nNo further exit was sent automatically. Check the broker's positions and "
              "order book and square off manually if needed."
        )
        logger.error(f"Square-off follow-up: {details}")
        self._notifier.risk_alert("Square Off — NOT confirmed flat", details)
