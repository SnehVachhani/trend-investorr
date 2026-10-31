"""
Trend-Investorr: Notification Manager

Sends alerts for trading events via multiple channels.
Currently supports console logging (always on) and can be
extended with Telegram, Discord, email, etc.

Notification types:
    - SIGNAL:    New signal detected (volume spike)
    - TRADE:     Order placed/filled
    - RISK:      Risk limit hit, circuit breaker tripped
    - SYSTEM:    Startup, shutdown, errors
    - DAILY:     End-of-day summary

All notifications are fire-and-forget — failures never
block the trading pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from loguru import logger

from core.constants import IST


class NotificationType(str, Enum):
    SIGNAL = "SIGNAL"
    TRADE = "TRADE"
    RISK = "RISK"
    SYSTEM = "SYSTEM"
    DAILY = "DAILY"


class NotificationPriority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class Notification:
    """A single notification to be dispatched."""
    type: NotificationType
    priority: NotificationPriority
    title: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(IST))


class Notifier:
    """
    Multi-channel notification dispatcher.

    All channels are fire-and-forget — failures are logged
    but never propagate to the caller.

    Usage:
        notifier = Notifier()
        notifier.signal_alert(signal)
        notifier.trade_alert(order_result)
        notifier.daily_summary(pnl_data)
    """

    def __init__(self, telegram_token: str = "", telegram_chat_id: str = ""):
        self._telegram_token = telegram_token
        self._telegram_chat_id = telegram_chat_id
        self._notifications_sent: int = 0

    def send(self, notification: Notification) -> None:
        """
        Dispatch a notification to all active channels.

        Never raises — all errors are caught and logged.
        """
        try:
            # Always log to console
            self._console_notify(notification)

            # Telegram (if configured)
            if self._telegram_token and self._telegram_chat_id:
                self._telegram_notify(notification)

            self._notifications_sent += 1

        except Exception as e:
            logger.warning(f"Notification dispatch failed (non-critical): {e}")

    # ── Convenience Methods ──────────────────────────────

    def signal_alert(self, symbol: str, signal_type: str, score: float,
                     volume_ratio: float, price: float, strength: str) -> None:
        """Alert for a new trading signal."""
        emoji = "🟢" if signal_type == "BUY" else "🔴"
        self.send(Notification(
            type=NotificationType.SIGNAL,
            priority=NotificationPriority.MEDIUM,
            title=f"{emoji} {signal_type} Signal: {symbol}",
            message=(
                f"Score: {score:.2f} | Strength: {strength}\n"
                f"Volume: {volume_ratio:.1f}x avg | Price: ₹{price:.2f}"
            ),
            data={"symbol": symbol, "signal_type": signal_type, "score": score},
        ))

    def trade_alert(self, symbol: str, side: str, quantity: int,
                    fill_price: float, is_paper: bool, pnl: float = 0) -> None:
        """Alert for an executed trade."""
        mode = "📄 PAPER" if is_paper else "💰 LIVE"
        pnl_str = f" | P&L: ₹{pnl:+,.0f}" if pnl != 0 else ""
        self.send(Notification(
            type=NotificationType.TRADE,
            priority=NotificationPriority.HIGH if not is_paper else NotificationPriority.MEDIUM,
            title=f"{mode} {side}: {symbol}",
            message=f"Qty: {quantity} | Price: ₹{fill_price:.2f}{pnl_str}",
            data={"symbol": symbol, "side": side, "quantity": quantity, "fill_price": fill_price},
        ))

    def risk_alert(self, title: str, details: str) -> None:
        """Alert for risk events (circuit breaker, loss limits)."""
        self.send(Notification(
            type=NotificationType.RISK,
            priority=NotificationPriority.CRITICAL,
            title=f"🛑 RISK: {title}",
            message=details,
        ))

    def system_alert(self, title: str, message: str,
                     priority: NotificationPriority = NotificationPriority.LOW) -> None:
        """Alert for system events (startup, shutdown, errors)."""
        self.send(Notification(
            type=NotificationType.SYSTEM,
            priority=priority,
            title=f"⚙️ {title}",
            message=message,
        ))

    def daily_summary(self, date_str: str, realized_pnl: float, unrealized_pnl: float,
                      total_trades: int, winning: int, losing: int,
                      portfolio_value: float, signals_generated: int) -> None:
        """End-of-day performance summary."""
        total_pnl = realized_pnl + unrealized_pnl
        emoji = "💚" if total_pnl >= 0 else "🔴"
        win_rate = (winning / max(total_trades, 1)) * 100

        self.send(Notification(
            type=NotificationType.DAILY,
            priority=NotificationPriority.MEDIUM,
            title=f"📊 Daily Summary — {date_str}",
            message=(
                f"{emoji} P&L: ₹{total_pnl:+,.0f} "
                f"(Realized: ₹{realized_pnl:+,.0f} | Unrealized: ₹{unrealized_pnl:+,.0f})\n"
                f"Trades: {total_trades} | Win Rate: {win_rate:.0f}% ({winning}W / {losing}L)\n"
                f"Portfolio: ₹{portfolio_value:,.0f} | Signals: {signals_generated}"
            ),
            data={
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_trades": total_trades,
                "portfolio_value": portfolio_value,
            },
        ))

    # ── Channels ─────────────────────────────────────────

    def _console_notify(self, n: Notification) -> None:
        """Log notification to console via loguru."""
        level = "INFO"
        if n.priority == NotificationPriority.CRITICAL:
            level = "WARNING"
        elif n.priority == NotificationPriority.HIGH:
            level = "INFO"

        logger.log(level, f"📢 [{n.type.value}] {n.title} — {n.message}")

    def _telegram_notify(self, n: Notification) -> None:
        """Send notification via Telegram Bot API."""
        try:
            import requests
            text = f"*{n.title}*\n{n.message}"
            requests.post(
                f"https://api.telegram.org/bot{self._telegram_token}/sendMessage",
                json={
                    "chat_id": self._telegram_chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
        except Exception as e:
            logger.debug(f"Telegram notification failed: {e}")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "notifications_sent": self._notifications_sent,
            "telegram_configured": bool(self._telegram_token),
        }
