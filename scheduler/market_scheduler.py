"""
Trend-Investorr: Market Lifecycle Scheduler

Orchestrates the 24/7 system lifecycle around market hours.

Daily Schedule (IST):
    08:30  — Pre-market boot (instrument reload, session login, config refresh)
    09:00  — Scanner warm-up (load historical volumes, subscribe WebSocket)
    09:15  — Market open → live scanning begins
    09:15-15:30 — Live trading loop (tick processing, signal generation)
    15:15  — Intraday square-off trigger
    15:30  — Market close → stop live scanning
    16:00  — Post-market analysis (bhavcopy download, volume recalculation)
    17:00  — Daily summary notification
    00:00  — Midnight: session re-login, circuit breaker reset

Architecture:
    Uses APScheduler with IST timezone.
    Jobs are idempotent — safe to restart at any time.
    Each job has error handling — one failure doesn't kill the scheduler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from core.constants import AuditEventType, IST
from core.security.audit import AuditTrail


class MarketScheduler:
    """
    APScheduler-based lifecycle manager for the trading system.

    Usage:
        scheduler = MarketScheduler(audit=audit)
        scheduler.register_pre_market(pre_market_fn)
        scheduler.register_live_trading(live_trading_fn)
        scheduler.register_post_market(post_market_fn)
        scheduler.start()
    """

    def __init__(self, audit: AuditTrail | None = None):
        self._scheduler = BackgroundScheduler(
            timezone=IST,
            job_defaults={
                "coalesce": True,       # Skip missed runs
                "max_instances": 1,     # No parallel runs
                "misfire_grace_time": 300,  # 5 min grace
            },
        )
        self._audit = audit
        self._is_running = False
        self._job_registry: dict[str, str] = {}  # name -> job_id

    def register_pre_market(self, fn: Callable[[], None]) -> None:
        """
        Register pre-market boot tasks.

        Runs at 08:30 IST — instrument reload, session login, config refresh.
        """
        self._add_job(
            name="pre_market_boot",
            fn=fn,
            hour=8, minute=30,
            description="Pre-market: instrument reload, session login, config refresh",
        )

    def register_scanner_warmup(self, fn: Callable[[], None]) -> None:
        """
        Register scanner warm-up.

        Runs at 09:00 IST — load historical volumes, subscribe WebSocket.
        """
        self._add_job(
            name="scanner_warmup",
            fn=fn,
            hour=9, minute=0,
            description="Scanner warm-up: historical volumes, WebSocket subscribe",
        )

    def register_market_open(self, fn: Callable[[], None]) -> None:
        """
        Register market open handler.

        Runs at 09:15 IST — start live scanning.
        """
        self._add_job(
            name="market_open",
            fn=fn,
            hour=9, minute=15,
            description="Market open: start live scanning",
        )

    def register_intraday_squareoff(self, fn: Callable[[], None]) -> None:
        """
        Register intraday square-off.

        Runs at 15:15 IST — 15 minutes before close.
        """
        self._add_job(
            name="intraday_squareoff",
            fn=fn,
            hour=15, minute=15,
            description="Intraday square-off",
        )

    def register_market_close(self, fn: Callable[[], None]) -> None:
        """
        Register market close handler.

        Runs at 15:30 IST — stop live scanning.
        """
        self._add_job(
            name="market_close",
            fn=fn,
            hour=15, minute=30,
            description="Market close: stop live scanning",
        )

    def register_post_market(self, fn: Callable[[], None]) -> None:
        """
        Register post-market analysis.

        Runs at 16:00 IST — bhavcopy download, volume recalculation.
        """
        self._add_job(
            name="post_market_analysis",
            fn=fn,
            hour=16, minute=0,
            description="Post-market: bhavcopy, volume recalculation",
        )

    def register_daily_summary(self, fn: Callable[[], None]) -> None:
        """
        Register daily summary notification.

        Runs at 17:00 IST — send daily P&L summary.
        """
        self._add_job(
            name="daily_summary",
            fn=fn,
            hour=17, minute=0,
            description="Daily P&L summary notification",
        )

    def register_midnight_reset(self, fn: Callable[[], None]) -> None:
        """
        Register midnight reset tasks.

        Runs at 00:00 IST — session re-login, circuit breaker reset.
        """
        self._add_job(
            name="midnight_reset",
            fn=fn,
            hour=0, minute=0,
            description="Midnight: session re-login, daily reset",
        )

    def register_periodic(
        self,
        name: str,
        fn: Callable[[], None],
        minutes: int = 5,
        description: str = "",
    ) -> None:
        """
        Register a periodic job (e.g., heartbeat, config refresh).

        Args:
            name: Unique job name.
            fn: Function to call.
            minutes: Interval in minutes.
            description: Human-readable description.
        """
        wrapped = self._wrap_job(name, fn)
        job = self._scheduler.add_job(
            wrapped,
            trigger="interval",
            minutes=minutes,
            id=name,
            name=description or name,
            replace_existing=True,
        )
        self._job_registry[name] = job.id
        logger.debug(f"Periodic job registered: {name} (every {minutes}m)")

    def _add_job(
        self,
        name: str,
        fn: Callable[[], None],
        hour: int,
        minute: int,
        description: str = "",
        day_of_week: str = "mon-fri",
    ) -> None:
        """Add a cron-scheduled job (weekdays only by default)."""
        wrapped = self._wrap_job(name, fn)
        job = self._scheduler.add_job(
            wrapped,
            trigger=CronTrigger(
                hour=hour,
                minute=minute,
                day_of_week=day_of_week,
                timezone=IST,
            ),
            id=name,
            name=description or name,
            replace_existing=True,
        )
        self._job_registry[name] = job.id
        logger.debug(
            f"Job registered: {name} at {hour:02d}:{minute:02d} ({day_of_week}) — {description}"
        )

    def _wrap_job(self, name: str, fn: Callable[[], None]) -> Callable[[], None]:
        """Wrap a job function with error handling and audit logging."""
        def wrapped():
            start_time = datetime.now(IST)
            try:
                logger.info(f"⏰ Job started: {name}")

                if self._audit:
                    self._audit.log(
                        AuditEventType.SCHEDULER_TASK,
                        {"job": name, "action": "started", "time": start_time.isoformat()},
                    )

                fn()

                elapsed = (datetime.now(IST) - start_time).total_seconds()
                logger.info(f"✅ Job completed: {name} ({elapsed:.1f}s)")

                if self._audit:
                    self._audit.log(
                        AuditEventType.SCHEDULER_TASK,
                        {"job": name, "action": "completed", "elapsed_seconds": elapsed},
                    )

            except Exception as e:
                elapsed = (datetime.now(IST) - start_time).total_seconds()
                logger.error(f"❌ Job failed: {name} ({elapsed:.1f}s) — {e}")

                if self._audit:
                    self._audit.log(
                        AuditEventType.ERROR,
                        {
                            "job": name,
                            "action": "failed",
                            "error": str(e),
                            "elapsed_seconds": elapsed,
                        },
                    )

        wrapped.__name__ = f"job_{name}"
        return wrapped

    def start(self) -> None:
        """Start the scheduler."""
        if self._is_running:
            logger.warning("Scheduler already running")
            return

        self._scheduler.start()
        self._is_running = True

        logger.info(
            f"Scheduler started with {len(self._job_registry)} jobs: "
            + ", ".join(sorted(self._job_registry.keys()))
        )

        if self._audit:
            self._audit.log(
                AuditEventType.SYSTEM_STARTUP,
                {"component": "scheduler", "jobs": list(self._job_registry.keys())},
            )

    def stop(self) -> None:
        """Stop the scheduler gracefully."""
        if not self._is_running:
            return

        self._scheduler.shutdown(wait=True)
        self._is_running = False
        logger.info("Scheduler stopped")

        if self._audit:
            self._audit.log(
                AuditEventType.SYSTEM_SHUTDOWN,
                {"component": "scheduler"},
            )

    def run_now(self, job_name: str) -> None:
        """Manually trigger a job immediately."""
        job = self._scheduler.get_job(job_name)
        if job:
            logger.info(f"Manual trigger: {job_name}")
            job.modify(next_run_time=datetime.now(IST))
        else:
            logger.warning(f"Job not found: {job_name}")

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def jobs(self) -> list[dict[str, Any]]:
        """List all registered jobs with next run times."""
        result = []
        for job in self._scheduler.get_jobs():
            result.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return result

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "is_running": self._is_running,
            "total_jobs": len(self._job_registry),
            "jobs": list(self._job_registry.keys()),
        }
