"""Shared NSE/BSE stock-universe rules used by scanners, filters and algorithms.

The broker instrument master marks many ETFs as ``series = 'EQ'``.  Series
alone is therefore not enough to identify a company equity.  This module keeps
the database predicate and the in-memory equivalent together so every path
uses the same definition.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


# Index instruments that can be present in the NSE EQ feed without an ISIN.
_INDEX_SYMBOLS = frozenset({
    "NIFTY", "NIFTY50", "NIFTY 50", "NIFTYNXT50", "NIFTY NEXT 50",
    "BANKNIFTY", "BANK NIFTY", "FINNIFTY", "MIDCPNIFTY",
    "MIDCAP SELECT", "NIFTY MIDCAP SELECT", "SENSEX", "BSESN", "BSX",
})
_FUND_SYMBOL_RE = re.compile(
    r"(?:INAV|IETF|BEES|ETF(?:-EQ)?$|(?:GOLD|SILVER|NIFTY|SENSEX|VALUE)$)",
    re.IGNORECASE,
)
_TEST_RE = re.compile(r"NSETEST", re.IGNORECASE)


def _normalise_instrument_text(value: object) -> str:
    text = str(value or "").strip().upper()
    return re.sub(r"-EQ$", "", text)


def is_equity_stock(stock: Mapping[str, Any]) -> bool:
    """Return whether a stock row is a regular NSE or BSE company equity.

    ETF ISINs use the ``INF`` prefix.  A few quote-feed ETF/index rows have no
    ISIN, so the explicit symbol/name markers cover those without rejecting
    normal company names such as JETFREIGHT.
    """
    if str(stock.get("exchange") or "").upper() not in {"NSE", "BSE"}:
        return False
    if str(stock.get("series") or "EQ").upper() != "EQ":
        return False
    symbol = _normalise_instrument_text(stock.get("symbol"))
    name = _normalise_instrument_text(stock.get("name"))
    if _TEST_RE.search(symbol) or _TEST_RE.search(name):
        return False
    isin = str(stock.get("isin") or "").strip().upper()
    if isin.startswith("INF"):
        return False
    if symbol in _INDEX_SYMBOLS or name in _INDEX_SYMBOLS:
        return False
    token = str(stock.get("token") or "").strip()
    if token.startswith("999"):
        return False
    # BSE's cash instrument feed also includes debt securities with blank
    # instrumenttype values. Company equity scrip codes are in the 5xxxxx
    # range; the 7xxxxx-9xxxxx ranges contain bonds and other debt products.
    if stock.get("exchange", "").upper() == "BSE" and not token.startswith("5"):
        return False
    if not isin and (_FUND_SYMBOL_RE.search(symbol) or _FUND_SYMBOL_RE.search(name)):
        return False
    return True


def equity_universe_sql(alias: str = "s") -> str:
    """SQL predicate for the same universe as :func:`is_equity_stock`."""
    q = alias
    # Keep this as SQL text because it is inserted into several batched CTEs;
    # the alias is an internal constant supplied by application code.
    return f"""
        {q}.is_active = TRUE
        AND {q}.exchange IN ('NSE', 'BSE')
        AND COALESCE({q}.series, 'EQ') = 'EQ'
        AND LEFT(COALESCE({q}.token, ''), 3) <> '999'
        AND ({q}.exchange <> 'BSE' OR LEFT(COALESCE({q}.token, ''), 1) = '5')
        AND POSITION('NSETEST' IN UPPER(COALESCE({q}.symbol, ''))) = 0
        AND POSITION('NSETEST' IN UPPER(COALESCE({q}.name, ''))) = 0
        AND LEFT(UPPER(COALESCE({q}.isin, '')), 3) <> 'INF'
        AND NOT (
            UPPER(REGEXP_REPLACE(COALESCE({q}.symbol, ''), '-EQ$', '')) IN (
                'NIFTY', 'NIFTY50', 'NIFTY 50', 'NIFTYNXT50', 'NIFTY NEXT 50',
                'BANKNIFTY', 'BANK NIFTY', 'FINNIFTY', 'MIDCPNIFTY',
                'MIDCAP SELECT', 'NIFTY MIDCAP SELECT', 'SENSEX', 'BSESN', 'BSX'
            )
            OR UPPER(BTRIM(COALESCE({q}.name, ''))) IN (
                'NIFTY', 'NIFTY50', 'NIFTY 50', 'NIFTYNXT50', 'NIFTY NEXT 50',
                'BANKNIFTY', 'BANK NIFTY', 'FINNIFTY', 'MIDCPNIFTY',
                'MIDCAP SELECT', 'NIFTY MIDCAP SELECT', 'SENSEX', 'BSESN', 'BSX'
            )
            OR (
                NULLIF(BTRIM(COALESCE({q}.isin, '')), '') IS NULL
                AND (
                    UPPER(COALESCE({q}.symbol, '')) ~ '(INAV|IETF|BEES|ETF(-EQ)?$|(GOLD|SILVER|NIFTY|SENSEX|VALUE)$)'
                    OR UPPER(COALESCE({q}.name, '')) ~ '(INAV|IETF|BEES|ETF(-EQ)?$|(GOLD|SILVER|NIFTY|SENSEX|VALUE)$)'
                )
            )
        )
    """.strip()
