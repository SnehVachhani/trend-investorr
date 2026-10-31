"""Small, defensive client for Upstox's read-only fundamentals APIs.

The client deliberately contains no trading methods.  It owns HTTP concerns
(authentication, retries, rate-limit handling and response validation) while
the sync service owns scheduling, persistence and normalization.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any, Callable

import requests
from loguru import logger

from core.exceptions import APIError, DataFetchError, RateLimitError


class UpstoxFundamentalsClient:
    """Read-only Upstox Fundamentals API client."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.upstox.com/v2",
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        requests_per_minute: int = 500,
        requests_per_30_minutes: int = 2000,
        rate_sleep_fn: Callable[[float], None] | None = None,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ):
        if not token or not token.strip():
            raise ValueError("Upstox Analytics Token is required")
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._request_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._requests_per_minute = max(0, requests_per_minute)
        self._requests_per_30_minutes = max(0, requests_per_30_minutes)
        self._last_request_at: float | None = None
        self._minute_request_times: deque[float] = deque()
        self._half_hour_request_times: deque[float] = deque()
        self._rate_lock = Lock()
        self._session = session or requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "TrendInvestorr/1.0",
        })
        self._sleep = sleep_fn
        self._rate_sleep = rate_sleep_fn or time.sleep

    def get_profile(self, isin: str) -> dict[str, Any]:
        return self._get(f"/fundamentals/{isin}/profile")

    def get_balance_sheet(self, isin: str, statement_type: str = "consolidated") -> dict[str, Any]:
        return self._get(
            f"/fundamentals/{isin}/balance-sheet",
            params={"type": statement_type, "fs": "true"},
        )

    def get_cash_flow(self, isin: str, statement_type: str = "consolidated") -> dict[str, Any]:
        return self._get(
            f"/fundamentals/{isin}/cash-flow",
            params={"type": statement_type, "fs": "true"},
        )

    def get_income_statement(
        self,
        isin: str,
        time_period: str = "yearly",
        statement_type: str = "consolidated",
    ) -> dict[str, Any]:
        return self._get(
            f"/fundamentals/{isin}/income-statement",
            params={"type": statement_type, "time_period": time_period, "fs": "true"},
        )

    def get_share_holdings(self, isin: str) -> dict[str, Any]:
        return self._get(f"/fundamentals/{isin}/share-holdings")

    def get_key_ratios(self, isin: str) -> dict[str, Any]:
        return self._get(f"/fundamentals/{isin}/key-ratios")

    def get_corporate_actions(self, isin: str) -> dict[str, Any]:
        return self._get(f"/fundamentals/{isin}/corporate-actions")

    def get_competitors(self, instrument_key: str) -> dict[str, Any]:
        # The published fundamentals docs show an ISIN path, but the live
        # endpoint validates this parameter as an Upstox instrument key
        # (for example, NSE_EQ|INE002A01018).
        return self._get(f"/fundamentals/{instrument_key}/competitors")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET JSON with bounded retries and no credential leakage in logs."""
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                self._wait_for_rate_limit()
                response = self._session.get(url, params=params, timeout=self._timeout)

                if response.status_code == 429:
                    retry_after = self._retry_after(response)
                    last_error = RateLimitError(
                        "Upstox rate limit exceeded",
                        retry_after=retry_after,
                        endpoint=path,
                    )
                    if attempt < self._max_retries:
                        self._sleep(retry_after)
                        continue
                    raise last_error

                if response.status_code in {500, 502, 503, 504}:
                    last_error = APIError(
                        f"Upstox server error for {path}",
                        status_code=response.status_code,
                        endpoint=path,
                    )
                    if attempt < self._max_retries:
                        self._sleep(min(2 ** (attempt - 1), 8))
                        continue
                    raise last_error

                if response.status_code >= 400:
                    raise APIError(
                        f"Upstox request failed for {path}",
                        status_code=response.status_code,
                        endpoint=path,
                        response=response.text[:500],
                    )

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise DataFetchError(
                        f"Upstox returned invalid JSON for {path}",
                        details={"endpoint": path, "status_code": response.status_code},
                    ) from exc

                if not isinstance(payload, dict):
                    raise DataFetchError(
                        f"Upstox returned an unexpected response for {path}",
                        details={"endpoint": path, "response_type": type(payload).__name__},
                    )

                if payload.get("status") not in (None, "success"):
                    raise APIError(
                        f"Upstox returned an API error for {path}",
                        status_code=response.status_code,
                        endpoint=path,
                        response=str(payload)[:500],
                    )
                return payload

            except (APIError, RateLimitError, DataFetchError):
                raise
            except requests.RequestException as exc:
                last_error = DataFetchError(
                    f"Upstox transport error for {path}: {exc}",
                    details={"endpoint": path, "attempt": attempt},
                )
                if attempt < self._max_retries:
                    self._sleep(min(2 ** (attempt - 1), 8))
                    continue
                raise last_error from exc

        raise last_error or DataFetchError(f"Upstox request failed for {path}")

    def _wait_for_rate_limit(self) -> None:
        """Reserve a physical HTTP attempt under both rolling-window caps."""
        if (
            self._request_interval <= 0
            and self._requests_per_minute <= 0
            and self._requests_per_30_minutes <= 0
        ):
            return
        with self._rate_lock:
            while True:
                now = time.monotonic()
                minute_cutoff = now - 60.0
                half_hour_cutoff = now - 1800.0
                while self._minute_request_times and self._minute_request_times[0] <= minute_cutoff:
                    self._minute_request_times.popleft()
                while self._half_hour_request_times and self._half_hour_request_times[0] <= half_hour_cutoff:
                    self._half_hour_request_times.popleft()

                waits = []
                if self._last_request_at is not None and self._request_interval > 0:
                    waits.append(self._last_request_at + self._request_interval - now)
                if (
                    self._requests_per_minute > 0
                    and len(self._minute_request_times) >= self._requests_per_minute
                ):
                    waits.append(self._minute_request_times[0] + 60.0 - now)
                if (
                    self._requests_per_30_minutes > 0
                    and len(self._half_hour_request_times) >= self._requests_per_30_minutes
                ):
                    waits.append(self._half_hour_request_times[0] + 1800.0 - now)

                remaining = max(waits, default=0.0)
                if remaining > 0:
                    self._rate_sleep(remaining + 0.001)
                    continue

                reserved_at = time.monotonic()
                if self._requests_per_minute > 0:
                    self._minute_request_times.append(reserved_at)
                if self._requests_per_30_minutes > 0:
                    self._half_hour_request_times.append(reserved_at)
                self._last_request_at = reserved_at
                return

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        value = response.headers.get("Retry-After", "1")
        try:
            return max(0.1, min(float(value), 60.0))
        except (TypeError, ValueError):
            return 1.0

    def __repr__(self) -> str:
        return f"UpstoxFundamentalsClient(base_url={self._base_url!r})"
