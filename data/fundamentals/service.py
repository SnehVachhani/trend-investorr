"""Scheduled Upstox fundamentals ingestion service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from loguru import logger

from core.constants import IST
from data.fundamentals.normalizer import (
    extract_event_detail,
    normalize_label,
    parse_number,
    parse_period,
    payload_hash,
)
from data.fundamentals.instrument_resolver import UpstoxInstrumentResolver
from data.fundamentals.upstox_client import UpstoxFundamentalsClient
from storage.models import FundamentalStore, StockStore


@dataclass
class FundamentalsSyncStats:
    run_id: int | None = None
    requested_stocks: int = 0
    mapped_stocks: int = 0
    unique_isins: int = 0
    skipped_stocks: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    due_requests: int = 0
    pending_requests: int = 0
    already_running: bool = False
    batch_number: int | None = None
    endpoint_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def record_success(self, endpoint: str) -> None:
        self.successful_requests += 1
        bucket = self.endpoint_counts.setdefault(endpoint, {"success": 0, "failed": 0})
        bucket["success"] += 1

    def record_failure(self, endpoint: str, symbol: str, error: Exception) -> None:
        self.failed_requests += 1
        bucket = self.endpoint_counts.setdefault(endpoint, {"success": 0, "failed": 0})
        bucket["failed"] += 1
        if len(self.errors) < 50:
            self.errors.append({"endpoint": endpoint, "symbol": symbol, "error": str(error)[:500]})


class FundamentalsSyncService:
    """Fetch only data that is due, normalize it and persist an audit trail."""

    DAILY_ENDPOINTS = (
        "key_ratios",
        "corporate_actions",
    )
    WEEKLY_ENDPOINTS = (
        "income_statement_quarterly_consolidated",
        "income_statement_quarterly_standalone",
        "balance_sheet_consolidated",
        "balance_sheet_standalone",
        "cash_flow_consolidated",
        "cash_flow_standalone",
        "share_holdings",
    )
    MONTHLY_ENDPOINTS = (
        "profile",
        "competitors",
    )
    ENDPOINTS = DAILY_ENDPOINTS + WEEKLY_ENDPOINTS + MONTHLY_ENDPOINTS

    def __init__(
        self,
        client: UpstoxFundamentalsClient,
        resolver: UpstoxInstrumentResolver,
        fundamental_store: FundamentalStore,
        stock_store: StockStore,
        *,
        max_requests_per_run: int = 2000,
        max_stocks_per_run: int = 0,
        profile_refresh_days: int = 30,
        statement_refresh_days: int = 7,
        ratio_refresh_days: int = 1,
        shareholding_refresh_days: int = 7,
        corporate_action_refresh_days: int = 1,
        competitor_refresh_days: int = 30,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._resolver = resolver
        self._store = fundamental_store
        self._stock_store = stock_store
        self._max_requests = max(1, max_requests_per_run)
        self._max_stocks = max(0, max_stocks_per_run)
        self._refresh_days = {
            "profile": profile_refresh_days,
            "income_statement_quarterly_consolidated": statement_refresh_days,
            "income_statement_quarterly_standalone": statement_refresh_days,
            "balance_sheet_consolidated": statement_refresh_days,
            "balance_sheet_standalone": statement_refresh_days,
            "cash_flow_consolidated": statement_refresh_days,
            "cash_flow_standalone": statement_refresh_days,
            "share_holdings": shareholding_refresh_days,
            "key_ratios": ratio_refresh_days,
            "corporate_actions": corporate_action_refresh_days,
            "competitors": competitor_refresh_days,
        }
        self._now = now_fn or (lambda: datetime.now(IST))

    def sync_due(
        self,
        force: bool = False,
        batch_number: int | None = None,
        endpoints: Sequence[str] | None = None,
    ) -> FundamentalsSyncStats:
        selected_endpoints = tuple(dict.fromkeys(self.ENDPOINTS if endpoints is None else endpoints))
        unknown_endpoints = set(selected_endpoints) - set(self.ENDPOINTS)
        if unknown_endpoints:
            raise ValueError(f"Unsupported fundamentals endpoints: {sorted(unknown_endpoints)}")
        if not selected_endpoints:
            return FundamentalsSyncStats(batch_number=batch_number)
        now = self._now()
        stocks = self._stock_store.get_fundamental_universe(self._max_stocks)
        stats = FundamentalsSyncStats(requested_stocks=len(stocks), batch_number=batch_number)
        stats.run_id = self._store.start_run(len(stocks))
        if stats.run_id is None:
            stats.already_running = True
            return stats

        processed_stock_ids: set[int] = set()
        try:
            self._resolve_identities(stocks, stats)
            aliases_by_isin: dict[str, list[dict]] = {}
            for stock in stocks:
                isin = str(stock.get("isin") or "").strip().upper()
                if isin:
                    aliases_by_isin.setdefault(isin, []).append(stock)
            stats.unique_isins = len(aliases_by_isin)

            canonical_by_isin = {
                isin: sorted(
                    aliases,
                    key=lambda stock: (
                        stock.get("series") != "EQ",
                        not str(stock.get("upstox_instrument_key") or "").startswith("NSE_EQ|"),
                        str(stock.get("symbol") or ""),
                    ),
                )[0]
                for isin, aliases in aliases_by_isin.items()
            }
            request_count = 0
            due_by_endpoint: dict[str, list[dict]] = {}
            for endpoint in selected_endpoints:
                refresh_days = self._refresh_days[endpoint]
                cutoff = now - timedelta(days=refresh_days)
                due_rows = self._due_rows(endpoint, stocks, cutoff, force)
                seen_isins: set[str] = set()
                endpoint_jobs = []
                for due_stock in due_rows:
                    isin = str(due_stock.get("isin") or "").strip().upper()
                    if not isin or isin in seen_isins or isin not in aliases_by_isin:
                        continue
                    seen_isins.add(isin)
                    endpoint_jobs.append({
                        "stock": canonical_by_isin[isin],
                        "aliases": aliases_by_isin[isin],
                    })
                due_by_endpoint[endpoint] = endpoint_jobs
                stats.due_requests += len(endpoint_jobs)

            # Interleave endpoints so a full-universe backlog cannot let the
            # first endpoint consume every batch and starve later data types.
            offset = now.timetuple().tm_yday % len(selected_endpoints)
            endpoint_order = selected_endpoints[offset:] + selected_endpoints[:offset]
            cursors = {endpoint: 0 for endpoint in selected_endpoints}
            while request_count < self._max_requests:
                made_progress = False
                for endpoint in endpoint_order:
                    if request_count >= self._max_requests:
                        break
                    cursor = cursors[endpoint]
                    due = due_by_endpoint[endpoint]
                    if cursor >= len(due):
                        continue
                    job = due[cursor]
                    cursors[endpoint] += 1
                    request_count += 1
                    processed_stock_ids.update(alias["id"] for alias in job["aliases"])
                    made_progress = True
                    self._sync_group(job, endpoint, now, self._refresh_days[endpoint], stats)
                if not made_progress:
                    break

            stats.pending_requests = max(0, stats.due_requests - request_count)
            status = (
                "completed"
                if stats.failed_requests == 0 and stats.skipped_stocks == 0 and stats.pending_requests == 0
                else "partial"
            )
            self._store.finish_run(
                stats.run_id,
                status=status,
                processed_stocks=len(processed_stock_ids),
                successful_requests=stats.successful_requests,
                failed_requests=stats.failed_requests,
                skipped_stocks=stats.skipped_stocks,
                details={
                    "batch_number": batch_number,
                    "endpoint_counts": stats.endpoint_counts,
                    "due_requests": stats.due_requests,
                    "pending_requests": stats.pending_requests,
                    "unique_isins": stats.unique_isins,
                    "errors": stats.errors,
                },
            )
            return stats
        except Exception as exc:
            self._store.finish_run(
                stats.run_id,
                status="failed",
                processed_stocks=len(processed_stock_ids),
                successful_requests=stats.successful_requests,
                failed_requests=stats.failed_requests,
                skipped_stocks=stats.skipped_stocks,
                details={
                    "batch_number": batch_number,
                    "endpoint_counts": stats.endpoint_counts,
                    "due_requests": stats.due_requests,
                    "pending_requests": stats.pending_requests,
                    "unique_isins": stats.unique_isins,
                    "errors": stats.errors,
                },
                error_message=str(exc)[:2000],
            )
            raise

    def _resolve_identities(self, stocks: list[dict], stats: FundamentalsSyncStats) -> None:
        if not stocks:
            return
        try:
            self._resolver.load()
        except Exception as exc:
            logger.error(f"Unable to load Upstox instrument master: {exc}")
            stats.skipped_stocks = len(stocks)
            return

        for stock in stocks:
            if stock.get("isin"):
                stats.mapped_stocks += 1
                continue
            identity = self._resolver.resolve(stock.get("symbol", ""), stock.get("name", ""))
            if not identity:
                stats.skipped_stocks += 1
                logger.warning(f"No Upstox ISIN mapping for {stock.get('symbol')}")
                continue
            self._stock_store.update_upstox_identity(stock["id"], identity.isin, identity.instrument_key)
            stock["isin"] = identity.isin
            stock["upstox_instrument_key"] = identity.instrument_key
            stats.mapped_stocks += 1

    def _due_rows(self, endpoint: str, stocks: list[dict], cutoff: datetime, force: bool) -> list[dict]:
        mapped_ids = {stock["id"] for stock in stocks if stock.get("isin")}
        if not mapped_ids:
            return []
        # The store applies the same due check used by manual runs.  Filtering
        # to this run's universe prevents a limited run from touching another
        # caller's work.
        rows = self._store.get_due_stocks(endpoint, cutoff, limit=0)
        rows = [row for row in rows if row["id"] in mapped_ids]
        if force:
            rows = [stock for stock in stocks if stock.get("isin")]
        return rows

    def _sync_group(self, job: dict, endpoint: str, now: datetime, refresh_days: int, stats: FundamentalsSyncStats) -> None:
        aliases = job["aliases"]
        stock = job["stock"]
        isin = str(stock.get("isin") or "").strip().upper()
        if not isin:
            stats.skipped_stocks += len(aliases)
            return
        try:
            instrument_key = str(stock.get("upstox_instrument_key") or f"NSE_EQ|{isin}").strip()
            payload = self._fetch(endpoint, isin, instrument_key)
            normalized = self._normalize(endpoint, payload)
        except Exception as exc:
            stats.record_failure(endpoint, stock.get("symbol", ""), exc)
            logger.warning(f"Fundamentals sync failed: {stock.get('symbol')} {endpoint}: {exc}")
            for alias in aliases:
                try:
                    self._store.persist_failure(
                        stock_id=alias["id"],
                        endpoint=endpoint,
                        snapshot_date=now.date(),
                        error_message=str(exc),
                        next_due_at=now + timedelta(minutes=30),
                    )
                except Exception:
                    logger.exception(
                        f"Unable to persist fundamentals failure for {alias.get('symbol')} {endpoint}"
                    )
            return

        request_hash = payload_hash(payload)
        failed_aliases: list[tuple[dict, Exception]] = []
        for alias in aliases:
            try:
                self._store.persist_success(
                    stock_id=alias["id"],
                    isin=isin,
                    endpoint=endpoint,
                    snapshot_date=now.date(),
                    payload_hash=request_hash,
                    payload=payload,
                    normalized=normalized,
                    next_due_at=now + timedelta(days=refresh_days),
                )
            except Exception as exc:
                failed_aliases.append((alias, exc))
                try:
                    self._store.persist_failure(
                        stock_id=alias["id"],
                        endpoint=endpoint,
                        snapshot_date=now.date(),
                        error_message=str(exc),
                        next_due_at=now + timedelta(minutes=30),
                    )
                except Exception:
                    logger.exception(f"Unable to persist fundamentals failure for {alias.get('symbol')} {endpoint}")

        if failed_aliases:
            for alias, exc in failed_aliases:
                stats.record_failure(endpoint, alias.get("symbol", ""), exc)
        else:
            stats.record_success(endpoint)

    def _fetch(self, endpoint: str, isin: str, instrument_key: str) -> dict[str, Any]:
        if endpoint == "profile":
            return self._client.get_profile(isin)
        if endpoint.startswith("income_statement_"):
            _, _, time_period, statement_type = endpoint.split("_")
            return self._client.get_income_statement(
                isin,
                time_period=time_period,
                statement_type=statement_type,
            )
        if endpoint.startswith("balance_sheet_"):
            return self._client.get_balance_sheet(isin, statement_type=endpoint.rsplit("_", 1)[1])
        if endpoint.startswith("cash_flow_"):
            return self._client.get_cash_flow(isin, statement_type=endpoint.rsplit("_", 1)[1])
        if endpoint == "share_holdings":
            return self._client.get_share_holdings(isin)
        if endpoint == "key_ratios":
            return self._client.get_key_ratios(isin)
        if endpoint == "corporate_actions":
            return self._client.get_corporate_actions(isin)
        if endpoint == "competitors":
            return self._client.get_competitors(instrument_key)
        raise ValueError(f"Unsupported Upstox endpoint: {endpoint}")

    def _normalize(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data") or {}
        if endpoint == "profile":
            return self._normalize_profile(data)
        if endpoint.startswith("income_statement_"):
            return {"lines": self._normalize_statement_lines(data, "income_statement")}
        if endpoint.startswith("balance_sheet_"):
            return {"lines": self._normalize_statement_lines(data, "balance_sheet")}
        if endpoint.startswith("cash_flow_"):
            return {"lines": self._normalize_statement_lines(data, "cash_flow")}
        if endpoint == "key_ratios":
            return {"ratios": self._normalize_ratios(data)}
        if endpoint == "share_holdings":
            return {"shareholdings": self._normalize_shareholdings(data)}
        if endpoint == "corporate_actions":
            return {"actions": self._normalize_actions(data)}
        if endpoint == "competitors":
            return {"competitors": self._normalize_competitors(data)}
        return {}

    @staticmethod
    def _normalize_profile(data: dict[str, Any]) -> dict[str, Any]:
        def nested_value(key: str) -> float | None:
            item = data.get(key) or {}
            return parse_number(item.get("value")) if isinstance(item, dict) else parse_number(item)

        return {
            "company_profile": str(data.get("company_profile") or ""),
            "sector": str(data.get("sector") or ""),
            "sector_market_cap_inr": nested_value("sector_market_cap_inr"),
            "sector_market_cap_usd": nested_value("sector_market_cap_usd"),
        }

    @staticmethod
    def _normalize_statement_lines(data: dict[str, Any], statement_kind: str) -> list[dict[str, Any]]:
        statement_type = str(data.get("type") or "consolidated")
        default_frequency = str(data.get("time_period") or "yearly")
        lines: list[dict[str, Any]] = []

        def add(metric_name: Any, history: Any, frequency: str) -> None:
            if not isinstance(history, list):
                return
            for item in history:
                if not isinstance(item, dict):
                    continue
                period_label = str(item.get("period") or "")
                if not period_label:
                    continue
                metric_name = normalize_label(metric_name)
                unit = str(data.get("units_in") or "crore")
                if metric_name.startswith("eps"):
                    unit = "rupees_per_share"
                lines.append({
                    "statement_type": statement_type,
                    "frequency": frequency,
                    "period_label": period_label,
                    "period_end": parse_period(period_label),
                    "metric_name": metric_name,
                    "value": parse_number(item.get("value")),
                    "change_pct": parse_number(item.get("change")),
                    "unit": unit,
                })

        category_key = {
            "income_statement": "income_statement",
            "balance_sheet": "history",
            "cash_flow": "cash_flow",
        }[statement_kind]
        if category_key == "history":
            add("total_asset", data.get("history"), default_frequency)
            # The balance-sheet summary has a second stable field.
            history = data.get("history") or []
            for item in history:
                if isinstance(item, dict) and "total_liability" in item:
                    add("total_liability", [{"period": item.get("period"), "value": item.get("total_liability")}], default_frequency)
        else:
            for category in data.get(category_key, []) or []:
                if isinstance(category, dict):
                    add(category.get("category"), category.get("history"), default_frequency)

        for item in data.get("full_statement", []) or []:
            if isinstance(item, dict):
                add(item.get("particular"), item.get("history"), "yearly")
        return lines

    @staticmethod
    def _normalize_ratios(data: Any) -> list[dict[str, Any]]:
        rows = data if isinstance(data, list) else []
        return [
            {
                "ratio_name": str(row.get("name") or ""),
                "company_value": parse_number(row.get("company_value")),
                "sector_value": parse_number(row.get("sector_value")),
                "company_raw": str(row.get("company_value") or ""),
                "sector_raw": str(row.get("sector_value") or ""),
            }
            for row in rows
            if isinstance(row, dict) and row.get("name")
        ]

    @staticmethod
    def _normalize_shareholdings(data: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for category in data if isinstance(data, list) else []:
            if not isinstance(category, dict):
                continue
            for item in category.get("history", []) or []:
                if not isinstance(item, dict) or not item.get("period"):
                    continue
                rows.append({
                    "category": str(category.get("category") or ""),
                    "period_label": str(item.get("period")),
                    "period_end": parse_period(item.get("period")),
                    "percentage": parse_number(item.get("value")),
                })
        return rows

    @staticmethod
    def _normalize_actions(data: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in data if isinstance(data, list) else []:
            if not isinstance(event, dict):
                continue
            action_type = str(event.get("name") or "")
            details = event.get("event_details") or []
            effective_text = event.get("expiry_date") or extract_event_detail(event, "Ex dividend date", "Ex date")
            announcement_text = extract_event_detail(event, "Announcement date")
            record_text = extract_event_detail(event, "Record date")
            detail_map = {
                normalize_label(item.get("name")): item.get("value")
                for item in details
                if isinstance(item, dict) and item.get("name")
            }
            stable = {
                "action_type": action_type,
                "effective": effective_text,
                "announcement": announcement_text,
                "record": record_text,
                "amount": event.get("amount") or detail_map.get("amount"),
                "ratio": event.get("ratio"),
            }
            event_key = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
            rows.append({
                "event_key": event_key,
                "action_type": action_type,
                "effective_date": parse_period(effective_text),
                "announcement_date": parse_period(announcement_text),
                "record_date": parse_period(record_text),
                "amount": parse_number(event.get("amount") or detail_map.get("amount")),
                "ratio": str(event.get("ratio") or "") or None,
                "details": {"event": event, "detail_map": detail_map},
            })
        return rows

    @staticmethod
    def _normalize_competitors(data: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("instrument_key"):
                continue
            instrument_key = str(item.get("instrument_key"))
            competitor_isin = instrument_key.split("|", 1)[1] if "|" in instrument_key else None

            def mcap(key: str) -> float | None:
                value = item.get(key) or {}
                return parse_number(value.get("value")) if isinstance(value, dict) else parse_number(value)

            rows.append({
                "competitor_isin": competitor_isin,
                "competitor_instrument_key": instrument_key,
                "company_profile": str(item.get("company_profile") or ""),
                "sector": str(item.get("sector") or ""),
                "sector_market_cap_inr": mcap("sector_market_cap_inr"),
                "sector_market_cap_usd": mcap("sector_market_cap_usd"),
            })
        return rows
