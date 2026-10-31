"""Focused tests for the Upstox fundamentals ingestion boundary."""

from __future__ import annotations

import gzip
import json

import pytest

from core.exceptions import APIError, RateLimitError
from data.fundamentals.instrument_resolver import UpstoxInstrumentResolver
from data.fundamentals.service import FundamentalsSyncService
from data.fundamentals.upstox_client import UpstoxFundamentalsClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.content = b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_upstox_client_sends_read_only_authenticated_request():
    session = FakeSession([FakeResponse(payload={"status": "success", "data": []})])
    client = UpstoxFundamentalsClient("secret-token", session=session)

    payload = client.get_key_ratios("INE123456789")

    assert payload["status"] == "success"
    assert session.headers["Authorization"] == "Bearer secret-token"
    assert session.calls[0][0].endswith("/fundamentals/INE123456789/key-ratios")
    assert session.calls[0][1]["timeout"] == 20.0


def test_upstox_client_retries_rate_limit_and_server_errors():
    session = FakeSession([
        FakeResponse(status_code=429, headers={"Retry-After": "0.1"}),
        FakeResponse(status_code=503),
        FakeResponse(payload={"status": "success", "data": {"ok": True}}),
    ])
    sleeps = []
    client = UpstoxFundamentalsClient("token", session=session, sleep_fn=sleeps.append)

    assert client.get_profile("INE123456789")["data"]["ok"] is True
    assert len(session.calls) == 3
    assert sleeps == [pytest.approx(0.1), pytest.approx(2)]


def test_upstox_client_raises_after_final_rate_limit():
    session = FakeSession([FakeResponse(status_code=429)] * 3)
    client = UpstoxFundamentalsClient("token", session=session, sleep_fn=lambda _: None)

    with pytest.raises(RateLimitError):
        client.get_profile("INE123456789")


def test_upstox_client_rejects_non_success_payload():
    session = FakeSession([FakeResponse(payload={"status": "error", "errors": []})])
    client = UpstoxFundamentalsClient("token", session=session)

    with pytest.raises(APIError):
        client.get_profile("INE123456789")


def test_instrument_resolver_prefers_equity_listing(tmp_path):
    instruments = [
        {
            "segment": "NSE_EQ",
            "isin": "INEBE0000001",
            "instrument_key": "NSE_EQ|INEBE0000001",
            "trading_symbol": "ALPHA",
            "name": "Alpha Limited",
            "instrument_type": "BE",
        },
        {
            "segment": "NSE_EQ",
            "isin": "INEEQ0000001",
            "instrument_key": "NSE_EQ|INEEQ0000001",
            "trading_symbol": "ALPHA",
            "name": "Alpha Limited",
            "instrument_type": "EQ",
        },
        {
            "segment": "NSE_EQ",
            "isin": "INEGA0000001",
            "instrument_key": "NSE_EQ|INEGA0000001",
            "trading_symbol": "GAMMA",
            "name": "Gamma Limited",
            "instrument_type": "EQ",
        },
        {"segment": "NSE_INDEX", "trading_symbol": "NIFTY"},
    ]
    body = gzip.compress(json.dumps(instruments).encode())
    response = FakeResponse()
    response.content = body
    response.raise_for_status = lambda: None
    session = FakeSession([response])
    resolver = UpstoxInstrumentResolver(tmp_path, session=session)

    assert resolver.load() == 2
    assert resolver.resolve("ALPHA-EQ").isin == "INEEQ0000001"
    assert resolver.resolve("", "Gamma Limited").isin == "INEGA0000001"


def test_fundamentals_normalizer_keeps_quarterly_and_yearly_lines_distinct():
    service = FundamentalsSyncService(None, None, None, None)  # type: ignore[arg-type]
    payload = {
        "status": "success",
        "data": {
            "time_period": "quarterly",
            "income_statement": [
                {
                    "category": "Revenue",
                    "history": [
                        {"period": "Jun 2025", "value": "125.5", "change": "10.2"},
                    ],
                },
            ],
            "full_statement": [],
        },
    }

    normalized = service._normalize("income_statement_quarterly", payload)

    assert normalized["lines"] == [
        {
            "statement_type": "consolidated",
            "frequency": "quarterly",
            "period_label": "Jun 2025",
            "period_end": normalized["lines"][0]["period_end"],
            "metric_name": "revenue",
            "value": 125.5,
            "change_pct": 10.2,
            "unit": "crore",
        }
    ]


def test_normalizer_captures_quarterly_shareholding_and_corporate_action_dates():
    service = FundamentalsSyncService(None, None, None, None)  # type: ignore[arg-type]
    holdings = service._normalize(
        "share_holdings",
        {"status": "success", "data": [{"category": "Promoters", "history": [{"period": "Jun 2025", "value": "52.4"}]}]},
    )
    actions = service._normalize(
        "corporate_actions",
        {"status": "success", "data": [{"name": "Dividend", "expiry_date": "14 Aug 2025", "amount": "2.5"}]},
    )

    assert holdings["shareholdings"][0]["percentage"] == 52.4
    assert str(actions["actions"][0]["effective_date"]) == "2025-08-14"
    assert actions["actions"][0]["amount"] == 2.5
