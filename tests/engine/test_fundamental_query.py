"""Rules, point-in-time inputs, and coverage for dynamic fundamental universes."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from engine.fundamental_query import (
    evaluate_universe,
    metrics_and_diagnostics_from_snapshots,
    metrics_from_snapshots,
    render_query,
    validate_rules,
)


def _income_snapshot(
    stock_id: int,
    fetched: datetime,
    annual: list[tuple[str, float]],
    quarterly: list[tuple[str, float]],
    *,
    endpoint: str = "income_statement_quarterly_consolidated",
) -> dict:
    return {
        "stock_id": stock_id,
        "endpoint": endpoint,
        "snapshot_date": fetched.date(),
        "fetched_at": fetched,
        "payload": {
            "status": "success",
            "data": {
                "type": "consolidated" if "consolidated" in endpoint else "standalone",
                "time_period": "quarterly",
                "units_in": "crore",
                "income_statement": [
                    {"category": "net_profit", "history": [
                        {"period": period, "value": value} for period, value in quarterly
                    ]},
                ],
                "full_statement": [
                    {"particular": "Profit After Tax", "history": [
                        {"period": period, "value": value} for period, value in annual
                    ]},
                ],
            },
        },
    }


def _ratio_snapshot(stock_id: int, fetched: datetime, *, pe=10, roe=18, roce=22) -> dict:
    return {
        "stock_id": stock_id,
        "endpoint": "key_ratios",
        "snapshot_date": fetched.date(),
        "fetched_at": fetched,
        "payload": {
            "status": "success",
            "data": [
                {"name": "P/E", "company_value": str(pe)},
                {"name": "ROE", "company_value": str(roe)},
                {"name": "ROCE", "company_value": str(roce)},
            ],
        },
    }


def test_rule_validation_and_readable_query_are_exact():
    rules = validate_rules([
        {"metric": "profit_growth_1y_pct", "operator": "gte", "value": 12},
        {"metric": "market_cap_cr", "operator": ">", "value": 500.5},
    ])
    assert rules == [
        {"metric": "profit_growth_1y_pct", "operator": ">=", "value": 12.0},
        {"metric": "market_cap_cr", "operator": ">", "value": 500.5},
    ]
    assert render_query(rules) == (
        "All of: 1-year annual net profit growth ≥ 12% AND "
        "estimated market capitalisation > ₹500.5 Cr. Missing values exclude a stock."
    )
    with pytest.raises(ValueError, match="finite number"):
        validate_rules([{"metric": "roe_pct", "operator": ">=", "value": float("nan")}])
    with pytest.raises(ValueError, match="unsupported metric"):
        validate_rules([{"metric": "sql", "operator": ">=", "value": 1}])


def test_metric_math_matches_annual_and_quarterly_periods():
    fetched = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    annual = [
        ("Mar 2021", 100), ("Mar 2022", 120), ("Mar 2023", 150),
        ("Mar 2024", 180), ("Mar 2025", 220), ("Mar 2026", 300),
    ]
    quarterly = [
        ("Jun 2025", 10), ("Sep 2025", 12), ("Dec 2025", 15),
        ("Mar 2026", 20), ("Jun 2026", 25),
    ]
    metrics, source = metrics_from_snapshots(
        [_income_snapshot(1, fetched, annual, quarterly), _ratio_snapshot(1, fetched)],
        date(2026, 9, 24),
    )
    assert metrics["profit_cagr_5y_pct"] == pytest.approx((3 ** 0.2 - 1) * 100)
    assert metrics["profit_growth_1y_pct"] == pytest.approx((300 / 220 - 1) * 100)
    assert metrics["quarter_profit_yoy_pct"] == 150
    assert metrics["quarter_profit_qoq_pct"] == 25
    assert metrics["roe_pct"] == 18
    assert metrics["roce_pct"] == 22
    assert metrics["market_cap_cr"] == 3000
    assert source == "estimated_pe_x_annual_profit"


def test_short_histories_and_loss_making_growth_are_insufficient():
    fetched = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    short = _income_snapshot(
        1, fetched,
        [("Mar 2023", 100), ("Mar 2024", 130), ("Mar 2025", 140), ("Mar 2026", 160)],
        [("Sep 2025", 5), ("Dec 2025", 6), ("Mar 2026", 7), ("Jun 2026", 8)],
    )
    metrics, _ = metrics_from_snapshots([short], date(2026, 9, 24))
    assert metrics["profit_cagr_5y_pct"] is None
    assert metrics["quarter_profit_yoy_pct"] is None
    assert metrics["profit_growth_1y_pct"] == pytest.approx((160 / 140 - 1) * 100)

    loss = _income_snapshot(
        1, fetched,
        [("Mar 2025", -5), ("Mar 2026", 10)],
        [("Mar 2026", -1), ("Jun 2026", 2)],
    )
    metrics, _ = metrics_from_snapshots([loss], date(2026, 9, 24))
    assert metrics["profit_growth_1y_pct"] is None
    assert metrics["quarter_profit_qoq_pct"] is None


def test_consolidated_and_standalone_profit_histories_are_not_spliced():
    fetched = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    consolidated = _income_snapshot(1, fetched, [("Mar 2026", 100)], [])
    standalone = _income_snapshot(
        1, fetched, [("Mar 2025", 40)], [],
        endpoint="income_statement_quarterly_standalone",
    )
    metrics, _ = metrics_from_snapshots([consolidated, standalone], date(2026, 9, 24))
    assert metrics["latest_annual_profit_cr"] == 100
    assert metrics["profit_growth_1y_pct"] is None


def test_standalone_history_fills_a_metric_consolidated_cannot_calculate():
    fetched = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    consolidated = _income_snapshot(1, fetched, [("Mar 2026", 100)], [])
    standalone = _income_snapshot(
        1, fetched, [("Mar 2025", 40), ("Mar 2026", 60)], [],
        endpoint="income_statement_quarterly_standalone",
    )
    metrics, _ = metrics_from_snapshots([consolidated, standalone], date(2026, 9, 24))
    assert metrics["latest_annual_profit_cr"] == 100
    assert metrics["profit_growth_1y_pct"] == 50


def test_missing_diagnostics_distinguish_history_from_provider_gaps():
    fetched = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    short = _income_snapshot(
        1, fetched, [("Mar 2025", 100), ("Mar 2026", 120)], [],
    )
    metrics, _, diagnostics = metrics_and_diagnostics_from_snapshots(
        [short], date(2026, 9, 24),
    )
    assert metrics["profit_cagr_5y_pct"] is None
    assert diagnostics["profit_cagr_5y_pct"]["code"] == "insufficient_comparable_history"
    assert diagnostics["roe_pct"]["code"] == "no_snapshot"

    malformed = _income_snapshot(2, fetched, [], [])
    malformed["payload"]["data"]["full_statement"] = [{
        "particular": "Profit After Tax",
        "history": [{"period": "unknown period", "value": "not reported"}],
    }]
    metrics, _, diagnostics = metrics_and_diagnostics_from_snapshots(
        [malformed], date(2026, 9, 24),
    )
    assert metrics["latest_annual_profit_cr"] is None
    assert diagnostics["latest_annual_profit_cr"]["code"] == "profit_line_unusable"


def test_future_fetched_payload_cannot_enter_historical_universe():
    future = datetime(2026, 9, 25, 11, tzinfo=timezone.utc)
    metrics, source = metrics_from_snapshots(
        [
            _income_snapshot(1, future, [("Mar 2025", 100), ("Mar 2026", 200)], []),
            _ratio_snapshot(1, future),
        ],
        date(2026, 9, 24),
    )
    assert metrics["profit_growth_1y_pct"] is None
    assert metrics["market_cap_cr"] is None
    assert source is None


def test_stale_annual_quarterly_and_ratio_inputs_become_unavailable():
    as_of = date(2026, 9, 24)
    now = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    older_fetch = datetime(2026, 2, 20, 11, tzinfo=timezone.utc)
    stale_statements = _income_snapshot(
        1, now,
        [("Mar 2023", 100), ("Mar 2024", 130)],
        [("Sep 2025", 5), ("Dec 2025", 6)],
    )
    metrics, source = metrics_from_snapshots(
        [stale_statements, _ratio_snapshot(1, older_fetch)], as_of,
    )
    assert metrics["latest_annual_profit_cr"] is None
    assert metrics["latest_quarter_profit_cr"] is None
    assert metrics["profit_growth_1y_pct"] is None
    assert metrics["quarter_profit_qoq_pct"] is None
    assert metrics["roe_pct"] is None
    assert metrics["roce_pct"] is None
    assert metrics["market_cap_cr"] is None
    assert source is None


def test_universe_uses_raw_snapshots_and_separates_missing_from_failed():
    fetched = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    good = _income_snapshot(1, fetched, [("Mar 2025", 100), ("Mar 2026", 130)], [])
    weak = _income_snapshot(2, fetched, [("Mar 2025", 100), ("Mar 2026", 105)], [])
    partial = _income_snapshot(4, fetched, [("Mar 2025", 100), ("Mar 2026", 105)], [])

    class FakeDatabase:
        def __init__(self):
            self.queries = []

        def fetch_all(self, query, params=()):
            self.queries.append((query, params))
            if "FROM stocks" in query:
                return [
                    {"stock_id": 1, "symbol": "GOOD-EQ", "name": "Good"},
                    {"stock_id": 2, "symbol": "WEAK-EQ", "name": "Weak"},
                    {"stock_id": 3, "symbol": "NONE-EQ", "name": "No Data"},
                    {"stock_id": 4, "symbol": "PARTIAL-EQ", "name": "Partial"},
                ]
            return [good, weak, _ratio_snapshot(1, fetched), _ratio_snapshot(2, fetched), partial]

    db = FakeDatabase()
    result = evaluate_universe(
        db, [
            {"metric": "profit_growth_1y_pct", "operator": ">=", "value": 20},
            {"metric": "roe_pct", "operator": ">=", "value": 10},
        ],
        date(2026, 9, 24),
    )
    assert result["coverage"]["total"] == 4
    assert result["coverage"]["eligible"] == 1
    assert result["coverage"]["insufficient"] == 2
    assert result["coverage"]["failed"] == 1
    assert result["coverage"]["incomplete_but_ruled_out"] == 1
    assert result["coverage"]["unresolved"] == 1
    assert result["coverage"]["missing_by_metric"] == {
        "profit_growth_1y_pct": 1, "roe_pct": 2,
    }
    assert result["eligible_stock_ids"] == [1]
    assert result["stocks"][0]["symbol"] == "GOOD-EQ"
    assert next(stock for stock in result["missing_stocks"] if stock["symbol"] == "PARTIAL-EQ")["known_failures"][0]["metric"] == "profit_growth_1y_pct"
    assert len(db.queries) == 2
    assert "fundamental_raw_snapshots" in db.queries[1][0]
    assert "fetched_at < %s" in db.queries[1][0]
    assert "company_fundamental_lines" not in db.queries[1][0]
    assert "NSETEST" in db.queries[0][0]
