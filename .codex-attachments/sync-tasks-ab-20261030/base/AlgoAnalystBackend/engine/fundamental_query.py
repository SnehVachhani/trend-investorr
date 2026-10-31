"""Point-in-time, rule-based fundamental stock universe selection.

Only provider payloads fetched by ``as_of`` are evaluated. A financial period
end is not its publication date; querying the latest normalized statement rows
for an old backtest date would therefore introduce look-ahead bias.
"""

from __future__ import annotations

import calendar
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from data.fundamentals.service import FundamentalsSyncService
from core.universe import equity_universe_sql


IST = ZoneInfo("Asia/Kolkata")

# Public rule contract. Symbolic operators are canonical; word forms are also
# accepted so browser clients need not contain comparison glyphs.
METRIC_LABELS: dict[str, tuple[str, str]] = {
    "profit_cagr_5y_pct": ("5-year annual net profit CAGR", "%"),
    "profit_growth_1y_pct": ("1-year annual net profit growth", "%"),
    "quarter_profit_yoy_pct": ("quarterly net profit year-on-year growth", "%"),
    "quarter_profit_qoq_pct": ("quarterly net profit quarter-on-quarter growth", "%"),
    "roe_pct": ("return on equity (ROE)", "%"),
    "roce_pct": ("return on capital employed (ROCE)", "%"),
    "market_cap_cr": ("estimated market capitalisation", " Cr"),
    "latest_annual_profit_cr": ("latest annual net profit", " Cr"),
    "latest_quarter_profit_cr": ("latest quarterly net profit", " Cr"),
}

_OPERATORS = {
    ">=": ">=", ">": ">", "<=": "<=", "<": "<",
    "gte": ">=", "gt": ">", "lte": "<=", "lt": "<",
}
_DISPLAY_OPERATORS = {">=": "≥", ">": ">", "<=": "≤", "<": "<"}
_PROFIT_ANNUAL = {
    "netprofit", "netprofitaftertax", "profitaftertax", "profitaftertaxation",
    "profitfortheperiod", "profitfortheyear", "pat",
}
_PROFIT_QUARTER = _PROFIT_ANNUAL
_INCOME_ENDPOINTS = (
    "income_statement_quarterly_consolidated",
    "income_statement_yearly_consolidated",
    "income_statement_quarterly_standalone",
    "income_statement_yearly_standalone",
)
_ENDPOINTS = (*_INCOME_ENDPOINTS, "key_ratios")


def validate_rules(rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate a conjunction of threshold rules and return canonical JSON.

    Operators ``gte``, ``gt``, ``lte`` and ``lt`` are accepted aliases for
    ``>=``, ``>``, ``<=`` and ``<`` respectively. Unknown keys are rejected to
    keep the saved query's meaning stable as the API evolves.
    """
    if not isinstance(rules, (list, tuple)) or not 1 <= len(rules) <= 20:
        raise ValueError("Provide between 1 and 20 fundamental rules")
    canonical: list[dict[str, Any]] = []
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, Mapping):
            raise ValueError(f"Rule {index} must be an object")
        if set(rule) != {"metric", "operator", "value"}:
            raise ValueError(f"Rule {index} must contain only metric, operator, and value")
        metric = rule["metric"]
        if not isinstance(metric, str) or metric not in METRIC_LABELS:
            raise ValueError(f"Rule {index} has an unsupported metric")
        operator = rule["operator"]
        if not isinstance(operator, str) or operator not in _OPERATORS:
            raise ValueError(f"Rule {index} has an unsupported operator")
        raw_value = rule["value"]
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(f"Rule {index} value must be a finite number")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"Rule {index} value must be a finite number")
        canonical.append({"metric": metric, "operator": _OPERATORS[operator], "value": value})
    return canonical


def render_query(rules: Sequence[Mapping[str, Any]]) -> str:
    """Render the exact saved thresholds in the same order as their rules."""
    canonical = validate_rules(rules)
    clauses = []
    for rule in canonical:
        label, unit = METRIC_LABELS[rule["metric"]]
        value = f"{rule['value']:g}"
        if rule["metric"] in {"market_cap_cr", "latest_annual_profit_cr", "latest_quarter_profit_cr"}:
            value = "₹" + value
        clauses.append(f"{label} {_DISPLAY_OPERATORS[rule['operator']]} {value}{unit}")
    return "All of: " + " AND ".join(clauses) + ". Missing values exclude a stock."


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _calendar_year_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:  # 29 February
        return value.replace(year=value.year - years, day=28)


def _months_prior(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _annual_comparison(series: Mapping[date, float], years: int) -> float | None:
    if not series:
        return None
    latest_period = max(series)
    latest = series[latest_period]
    if latest <= 0:
        return None
    target = _calendar_year_before(latest_period, years)
    candidates = [(abs((period - target).days), period) for period in series if period < latest_period]
    candidates = [(distance, period) for distance, period in candidates if distance <= 45]
    if not candidates:
        return None
    _, prior_period = min(candidates)
    prior = series[prior_period]
    if prior <= 0:
        return None
    ratio = latest / prior
    return ((ratio ** (1 / years)) - 1) * 100 if years > 1 else (ratio - 1) * 100


def _quarter_index(value: date) -> int:
    return value.year * 4 + (value.month - 1) // 3


def _quarter_comparison(series: Mapping[date, float], quarters: int) -> float | None:
    if not series:
        return None
    latest_period = max(series)
    latest = series[latest_period]
    if latest <= 0:
        return None
    target = _quarter_index(latest_period) - quarters
    prior_periods = [period for period in series if _quarter_index(period) == target]
    if not prior_periods:
        return None
    prior = series[max(prior_periods)]
    return (latest / prior - 1) * 100 if prior > 0 else None


def _snapshot_available(row: Mapping[str, Any], as_of: date) -> bool:
    """Defence in depth in addition to the database's as-of predicate."""
    fetched = row.get("fetched_at")
    if not isinstance(fetched, datetime):
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=IST)
    if fetched.astimezone(IST).date() > as_of:
        return False
    snapshot_date = row.get("snapshot_date")
    return not isinstance(snapshot_date, date) or snapshot_date <= as_of


def _profit_series_by_basis(
    snapshots: Sequence[Mapping[str, Any]], as_of: date,
) -> tuple[dict[str, dict[str, dict[date, float]]], dict[str, dict[str, str]]]:
    """Return fresh PAT histories separately for consolidated and standalone.

    A calculation always uses one complete statement basis. If consolidated
    history cannot produce a particular metric, the caller may retry that
    metric against the standalone series; periods are never spliced together.
    """
    candidates: dict[str, dict[str, list[tuple[datetime, date, float]]]] = {
        frequency: {basis: [] for basis in ("consolidated", "standalone")}
        for frequency in ("yearly", "quarterly")
    }
    has_income_snapshot = False
    observed_profit_row = {
        frequency: {basis: False for basis in ("consolidated", "standalone")}
        for frequency in ("yearly", "quarterly")
    }
    for row in snapshots:
        endpoint = str(row.get("endpoint") or "")
        if endpoint not in _INCOME_ENDPOINTS or not _snapshot_available(row, as_of):
            continue
        has_income_snapshot = True
        basis = "consolidated" if "consolidated" in endpoint else "standalone"
        payload = row.get("payload") or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        # The provider's headline category is quarterly; detailed full
        # statement lines are annual even in its quarterly response.
        quarter_rows = [
            item for item in data.get("income_statement", []) or []
            if isinstance(item, dict)
            and _normalized_name(item.get("category")) in _PROFIT_QUARTER
        ]
        annual_rows = [
            item for item in data.get("full_statement", []) or []
            if isinstance(item, dict)
            and _normalized_name(item.get("particular")) in _PROFIT_ANNUAL
        ]
        default_frequency = str(data.get("time_period") or "yearly").lower()
        if quarter_rows and default_frequency in observed_profit_row:
            observed_profit_row[default_frequency][basis] = True
        if annual_rows:
            observed_profit_row["yearly"][basis] = True
        profit_only = {
            "type": data.get("type"),
            "time_period": data.get("time_period"),
            "units_in": data.get("units_in"),
            "income_statement": quarter_rows,
            "full_statement": annual_rows,
        }
        lines = FundamentalsSyncService._normalize_statement_lines(profit_only, "income_statement")
        fetched = row["fetched_at"]
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=IST)
        for line in lines:
            frequency = line.get("frequency")
            period = line.get("period_end")
            value = line.get("value")
            if (
                frequency not in candidates
                or not isinstance(period, date) or period > as_of
                or value is None or not math.isfinite(float(value))
                or str(line.get("unit") or "").lower() not in {"crore", "crores"}
            ):
                continue
            candidates[frequency][basis].append((fetched, period, float(value)))

    series: dict[str, dict[str, dict[date, float]]] = {}
    states: dict[str, dict[str, str]] = {}
    for frequency, basis_rows in candidates.items():
        series[frequency] = {}
        states[frequency] = {}
        max_age_months = 18 if frequency == "yearly" else 6
        for basis, rows in basis_rows.items():
            rows.sort(key=lambda item: item[0], reverse=True)
            raw: dict[date, float] = {}
            for _, period, value in rows:
                raw.setdefault(period, value)
            if raw and max(raw) < _months_prior(as_of, max_age_months):
                series[frequency][basis] = {}
                states[frequency][basis] = "stale_reported_data"
            elif raw:
                series[frequency][basis] = raw
                states[frequency][basis] = "available"
            else:
                series[frequency][basis] = {}
                states[frequency][basis] = (
                    "profit_line_unusable" if observed_profit_row[frequency][basis]
                    else "no_usable_profit_line" if has_income_snapshot
                    else "no_snapshot"
                )
    return series, states


def _profit_series_pair(
    snapshots: Sequence[Mapping[str, Any]], as_of: date,
) -> tuple[dict[date, float], dict[date, float]]:
    """Compatibility helper returning the preferred complete basis per period."""
    series, _ = _profit_series_by_basis(snapshots, as_of)
    yearly = series["yearly"]["consolidated"] or series["yearly"]["standalone"]
    quarterly = series["quarterly"]["consolidated"] or series["quarterly"]["standalone"]
    return yearly, quarterly


def _ratio_metrics_with_status(
    snapshots: Sequence[Mapping[str, Any]], as_of: date,
) -> tuple[dict[str, float], dict[str, str]]:
    rows = [
        row for row in snapshots
        if row.get("endpoint") == "key_ratios" and _snapshot_available(row, as_of)
    ]
    rows.sort(key=lambda row: row["fetched_at"], reverse=True)
    if not rows:
        reason = "no_snapshot" if not any(row.get("endpoint") == "key_ratios" for row in snapshots) else "no_snapshot"
        return {}, {key: reason for key in ("roe_pct", "roce_pct", "pe")}
    latest_fetched = rows[0]["fetched_at"]
    if latest_fetched.tzinfo is None:
        latest_fetched = latest_fetched.replace(tzinfo=IST)
    if latest_fetched.astimezone(IST).date() < _months_prior(as_of, 6):
        return {}, {key: "stale_reported_data" for key in ("roe_pct", "roce_pct", "pe")}
    payload = rows[0].get("payload") or {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return {}, {key: "empty_provider_response" for key in ("roe_pct", "roce_pct", "pe")}
    result: dict[str, float] = {}
    for row in FundamentalsSyncService._normalize_ratios(data):
        value = row.get("company_value")
        if value is None or not math.isfinite(float(value)):
            continue
        name = _normalized_name(row.get("ratio_name"))
        if name in {"roe", "returnonequity", "returnonshareholdersequity"}:
            result.setdefault("roe_pct", float(value))
        elif name in {"roce", "returnoncapitalemployed"}:
            result.setdefault("roce_pct", float(value))
        elif name in {"pe", "pricetoearnings", "priceearningsratio"}:
            result.setdefault("pe", float(value))
    statuses = {
        key: ("available" if key in result else "metric_not_reported")
        for key in ("roe_pct", "roce_pct", "pe")
    }
    return result, statuses


def _ratio_metrics(snapshots: Sequence[Mapping[str, Any]], as_of: date) -> dict[str, float]:
    """Backward-compatible value-only ratio helper."""
    return _ratio_metrics_with_status(snapshots, as_of)[0]


def _growth_failure_reason(
    series: Mapping[date, float], *, years: int | None = None, quarters: int | None = None,
) -> str | None:
    if not series:
        return None
    latest_period = max(series)
    if series[latest_period] <= 0:
        return "nonpositive_profit_not_comparable"
    if years is not None:
        target = _calendar_year_before(latest_period, years)
        candidates = [period for period in series if period < latest_period and abs((period - target).days) <= 45]
        if not candidates:
            return "insufficient_comparable_history"
        prior_period = min(candidates, key=lambda period: abs((period - target).days))
    else:
        target = _quarter_index(latest_period) - int(quarters or 0)
        candidates = [period for period in series if _quarter_index(period) == target]
        if not candidates:
            return "insufficient_comparable_history"
        prior_period = max(candidates)
    if series[prior_period] <= 0:
        return "nonpositive_profit_not_comparable"
    return None


_MISSING_REASON_LABELS = {
    "no_instrument_mapping": "No provider instrument mapping (ISIN)",
    "no_snapshot": "No successful provider snapshot captured by this date",
    "no_usable_profit_line": "Provider response has no net-profit line at the required frequency",
    "profit_line_unusable": "Net-profit row exists, but its period, value, or unit cannot be read",
    "stale_reported_data": "Latest reported period is too old",
    "insufficient_comparable_history": "Not enough matching periods for this comparison",
    "nonpositive_profit_not_comparable": "Zero or negative profit prevents a meaningful growth comparison",
    "empty_provider_response": "Provider returned an empty ratio response",
    "metric_not_reported": "Provider response does not include this ratio",
    "roe_not_reported": "Provider response does not include ROE",
    "roce_not_reported": "Provider response does not include ROCE",
    "pe_not_reported": "Provider response does not include P/E",
    "nonpositive_profit_for_cap": "Annual profit is not positive, so P/E × profit cannot estimate market cap",
    "positive_pe_unavailable": "A positive P/E value is not available for the market-cap estimate",
}


def _reason_detail(code: str, basis: str | None = None) -> dict[str, str | None]:
    return {"code": code, "label": _MISSING_REASON_LABELS.get(code, code.replace("_", " ")), "basis": basis}


def metrics_and_diagnostics_from_snapshots(
    snapshots: Sequence[Mapping[str, Any]], as_of: date, *, has_instrument_mapping: bool = True,
) -> tuple[dict[str, float | None], str | None, dict[str, dict[str, str | None]]]:
    """Calculate metrics and explain every unavailable value."""
    series, states = _profit_series_by_basis(snapshots, as_of)
    ratios, ratio_status = _ratio_metrics_with_status(snapshots, as_of)
    bases = ("consolidated", "standalone")
    diagnostics: dict[str, dict[str, str | None]] = {}

    def select_growth(metric: str, frequency: str, calculator: Any, reason_args: dict[str, int]) -> float | None:
        failures: list[tuple[str, str]] = []
        for basis in bases:
            basis_series = series[frequency][basis]
            value = calculator(basis_series)
            if value is not None:
                return value
            state = states[frequency][basis]
            reason = state if state != "available" else _growth_failure_reason(basis_series, **reason_args)
            if reason:
                failures.append((reason, basis))
        if failures:
            priority = {
                "nonpositive_profit_not_comparable": 0,
                "insufficient_comparable_history": 1,
                "stale_reported_data": 2,
                "profit_line_unusable": 3,
                "no_usable_profit_line": 4,
                "no_snapshot": 5,
            }
            reason, basis = min(failures, key=lambda item: priority.get(item[0], 5))
        else:
            reason, basis = "no_snapshot", None
        diagnostics[metric] = _reason_detail(
            "no_instrument_mapping" if not has_instrument_mapping else reason,
            basis if has_instrument_mapping else None,
        )
        return None

    def select_latest(metric: str, frequency: str) -> float | None:
        failures: list[tuple[str, str]] = []
        for basis in bases:
            basis_series = series[frequency][basis]
            if basis_series:
                return basis_series[max(basis_series)]
            state = states[frequency][basis]
            if state:
                failures.append((state, basis))
        reason, basis = next(
            ((reason, basis) for reason in (
                "stale_reported_data", "profit_line_unusable", "no_usable_profit_line", "no_snapshot",
            )
             for candidate_reason, basis in failures if candidate_reason == reason),
            ("no_snapshot", None),
        )
        diagnostics[metric] = _reason_detail(
            "no_instrument_mapping" if not has_instrument_mapping else reason,
            basis if has_instrument_mapping else None,
        )
        return None

    latest_annual = select_latest("latest_annual_profit_cr", "yearly")
    latest_quarter = select_latest("latest_quarter_profit_cr", "quarterly")
    metrics: dict[str, float | None] = {
        "profit_cagr_5y_pct": select_growth(
            "profit_cagr_5y_pct", "yearly", lambda value: _annual_comparison(value, 5), {"years": 5},
        ),
        "profit_growth_1y_pct": select_growth(
            "profit_growth_1y_pct", "yearly", lambda value: _annual_comparison(value, 1), {"years": 1},
        ),
        "quarter_profit_yoy_pct": select_growth(
            "quarter_profit_yoy_pct", "quarterly", lambda value: _quarter_comparison(value, 4), {"quarters": 4},
        ),
        "quarter_profit_qoq_pct": select_growth(
            "quarter_profit_qoq_pct", "quarterly", lambda value: _quarter_comparison(value, 1), {"quarters": 1},
        ),
        "roe_pct": ratios.get("roe_pct"),
        "roce_pct": ratios.get("roce_pct"),
        "market_cap_cr": None,
        "latest_annual_profit_cr": latest_annual,
        "latest_quarter_profit_cr": latest_quarter,
    }
    for metric in ("roe_pct", "roce_pct"):
        if metrics[metric] is None:
            reason = ratio_status[metric]
            if reason == "metric_not_reported":
                reason = "roe_not_reported" if metric == "roe_pct" else "roce_not_reported"
            diagnostics[metric] = _reason_detail(
                "no_instrument_mapping" if not has_instrument_mapping else reason,
            )
    pe = ratios.get("pe")
    if latest_annual is not None and latest_annual > 0 and pe is not None and pe > 0:
        metrics["market_cap_cr"] = pe * latest_annual
        market_cap_source = "estimated_pe_x_annual_profit"
    else:
        market_cap_source = None
        if latest_annual is None:
            annual_reason = diagnostics.get("latest_annual_profit_cr", _reason_detail("no_snapshot"))
            diagnostics["market_cap_cr"] = dict(annual_reason)
        elif latest_annual <= 0:
            diagnostics["market_cap_cr"] = _reason_detail("nonpositive_profit_for_cap")
        else:
            reason = ratio_status["pe"]
            if reason == "metric_not_reported":
                reason = "pe_not_reported"
            elif reason == "available":
                reason = "positive_pe_unavailable"
            diagnostics["market_cap_cr"] = _reason_detail(
                "no_instrument_mapping" if not has_instrument_mapping else reason,
            )
    return metrics, market_cap_source, diagnostics


def metrics_from_snapshots(
    snapshots: Sequence[Mapping[str, Any]], as_of: date,
) -> tuple[dict[str, float | None], str | None]:
    """Calculate every supported metric from provider payloads known by date."""
    metrics, market_cap_source, _ = metrics_and_diagnostics_from_snapshots(snapshots, as_of)
    return metrics, market_cap_source


def _matches(actual: float, operator: str, threshold: float) -> bool:
    return {
        ">=": actual >= threshold,
        ">": actual > threshold,
        "<=": actual <= threshold,
        "<": actual < threshold,
    }[operator]


def evaluate_universe(
    db: Any,
    rules: Sequence[Mapping[str, Any]],
    as_of: date,
    *,
    include_missing_stocks: bool = True,
) -> dict[str, Any]:
    """Screen active NSE EQ stocks with two batched, point-in-time queries.

    Missing data is reported per metric and per reason. Incomplete stocks that
    already fail a known rule are still marked incomplete, but called out
    separately so the headline data-gap count remains honest.
    """
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise ValueError("as_of must be a calendar date")
    canonical = validate_rules(rules)
    stocks = db.fetch_all(
        f"""
        SELECT id AS stock_id, symbol, name, isin
        FROM stocks
        WHERE {equity_universe_sql('stocks')}
        ORDER BY symbol
        """
    )
    stock_ids = [int(stock["stock_id"]) for stock in stocks]
    snapshots: list[dict[str, Any]] = []
    archive_info: dict[str, Any] = {}
    if stock_ids:
        # Historical records entered on the as-of date are usable; a record
        # entered on the following date is not. Timestamptz comparison uses
        # the actual IST boundary, including for API callers outside India.
        end_exclusive = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=IST)
        snapshots = db.fetch_all(
            """
            SELECT DISTINCT ON (r.stock_id, r.endpoint)
                   r.stock_id, r.endpoint, r.payload, r.fetched_at, r.snapshot_date
            FROM fundamental_raw_snapshots r
            WHERE r.stock_id = ANY(%s)
              AND r.endpoint = ANY(%s)
              AND r.status = 'success'
              AND r.snapshot_date <= %s
              AND r.fetched_at < %s
            ORDER BY r.stock_id, r.endpoint, r.snapshot_date DESC,
                     r.fetched_at DESC, r.id DESC
            """,
            (stock_ids, list(_ENDPOINTS), as_of, end_exclusive),
        )
        fetch_one = getattr(db, "fetch_one", None)
        if callable(fetch_one):
            archive_info = fetch_one(
                """
                SELECT MIN(r.fetched_at) AS first_fetched_at,
                       COUNT(DISTINCT r.stock_id) AS archived_stocks
                FROM fundamental_raw_snapshots r
                WHERE r.stock_id = ANY(%s)
                  AND r.endpoint = ANY(%s)
                  AND r.status = 'success'
                """,
                (stock_ids, list(_ENDPOINTS)),
            ) or {}
    by_stock: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        by_stock[int(snapshot["stock_id"])].append(snapshot)
    snapshot_stock_count = len(by_stock)

    eligible: list[dict[str, Any]] = []
    insufficient = 0
    failed = 0
    incomplete_but_ruled_out = 0
    unresolved = 0
    missing_by_metric: Counter[str] = Counter()
    missing_by_metric_reason: dict[str, Counter[str]] = defaultdict(Counter)
    missing_reason_counts: Counter[str] = Counter()
    missing_stocks: list[dict[str, Any]] = []
    for stock in stocks:
        stock_id = int(stock["stock_id"])
        metrics, market_cap_source, diagnostics = metrics_and_diagnostics_from_snapshots(
            by_stock[stock_id], as_of,
            has_instrument_mapping=bool(stock.get("isin")),
        )
        missing = {rule["metric"] for rule in canonical if metrics[rule["metric"]] is None}
        if missing:
            insufficient += 1
            missing_by_metric.update(missing)
            missing_details = []
            for metric in sorted(missing):
                detail = diagnostics.get(metric) or _reason_detail("no_snapshot")
                code = str(detail.get("code") or "no_snapshot")
                missing_by_metric_reason[metric][code] += 1
                missing_reason_counts[code] += 1
                missing_details.append({"metric": metric, **detail})
            known_failures = [
                {
                    "metric": rule["metric"],
                    "label": METRIC_LABELS[rule["metric"]][0],
                    "operator": rule["operator"],
                    "threshold": rule["value"],
                    "actual": metrics[rule["metric"]],
                }
                for rule in canonical
                if metrics[rule["metric"]] is not None
                and not _matches(float(metrics[rule["metric"]]), rule["operator"], rule["value"])
            ]
            if known_failures:
                incomplete_but_ruled_out += 1
            else:
                unresolved += 1
            if include_missing_stocks:
                missing_stocks.append({
                    "stock_id": stock_id,
                    "symbol": stock["symbol"],
                    "name": stock.get("name") or stock["symbol"],
                    "missing": missing_details,
                    "known_failures": known_failures,
                })
            continue
        if not all(
            _matches(float(metrics[rule["metric"]]), rule["operator"], rule["value"])
            for rule in canonical
        ):
            failed += 1
            continue
        eligible.append({
            "stock_id": stock_id,
            "symbol": stock["symbol"],
            "name": stock.get("name") or stock["symbol"],
            "metrics": metrics,
            "market_cap_source": market_cap_source,
        })

    limitations = [
        "Historical membership starts only when a provider snapshot was recorded; earlier financial periods are not treated as known before that fetch.",
        "Annual and quarterly growth require matching reported periods and a positive starting and latest profit; unavailable comparisons are excluded.",
        "Annual profit must have a period end within 18 months, and quarterly profit and ratio snapshots within 6 months, of the screen date.",
    ]
    first_fetched_at = archive_info.get("first_fetched_at")
    if isinstance(first_fetched_at, datetime):
        if first_fetched_at.tzinfo is None:
            first_fetched_at = first_fetched_at.replace(tzinfo=IST)
        first_fetched_date = first_fetched_at.astimezone(IST).date()
        if first_fetched_date > as_of:
            limitations.append(
                f"No fundamentals snapshot had been captured by {as_of.isoformat()}; the first successful capture in this stock universe was {first_fetched_date.isoformat()}. Older statement periods inside that later snapshot cannot establish what was known at the historical cutoff."
            )
    if any(rule["metric"] == "market_cap_cr" for rule in canonical):
        limitations.append(
            "Market capitalisation is an estimate: as-of P/E multiplied by latest known annual net profit in crore. It is not a reported company market cap."
        )
    return {
        "as_of": as_of.isoformat(),
        "rules": canonical,
        "query_text": render_query(canonical),
        "coverage": {
            "total": len(stocks),
            "eligible": len(eligible),
            "insufficient": insufficient,
            "failed": failed,
            "missing_by_metric": dict(sorted(missing_by_metric.items())),
            "missing_by_metric_reason": {
                metric: dict(sorted(reasons.items()))
                for metric, reasons in sorted(missing_by_metric_reason.items())
            },
            "missing_reason_counts": dict(sorted(missing_reason_counts.items())),
            "missing_reason_labels": _MISSING_REASON_LABELS,
            "incomplete_but_ruled_out": incomplete_but_ruled_out,
            "unresolved": unresolved,
        },
        "snapshot_coverage": {
            "stocks_with_snapshots_as_of": snapshot_stock_count,
            "archived_stocks": int(
                archive_info["archived_stocks"]
                if archive_info.get("archived_stocks") is not None
                else snapshot_stock_count
            ),
            "first_fetched_at": first_fetched_at.isoformat() if isinstance(first_fetched_at, datetime) else None,
        },
        "stocks": eligible,
        "missing_stocks": missing_stocks,
        "eligible_stock_ids": [stock["stock_id"] for stock in eligible],
        "limitations": limitations,
    }
