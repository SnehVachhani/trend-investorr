#!/usr/bin/env python3
"""Refresh the built-in index watchlists from official constituent files.

The watchlist records are seeded by ``storage.database``.  This command keeps
their membership current without touching manually added stocks.  It refreshes
only items whose source is ``index_sync`` and uses the current NSE/BSE public
constituent endpoints.

Usage:
    python scripts/sync_index_watchlists.py
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings
from storage.database import Database


NSE_ARCHIVE = "https://nsearchives.nseindia.com/content/indices/"
NSE_SOURCES = {
    "Nifty 50": "ind_nifty50list.csv",
    "Nifty Next 50": "ind_niftynext50list.csv",
    "Nifty 100": "ind_nifty100list.csv",
    "Nifty 200": "ind_nifty200list.csv",
    "Nifty 500": "ind_nifty500list.csv",
    "Nifty Bank": "ind_niftybanklist.csv",
    "Nifty Financial Services": "ind_niftyfinancelist.csv",
    "Nifty IT": "ind_niftyitlist.csv",
    "Nifty Pharma": "ind_niftypharmalist.csv",
    "Nifty Auto": "ind_niftyautolist.csv",
    "Nifty FMCG": "ind_niftyfmcglist.csv",
    "Nifty Metal": "ind_niftymetallist.csv",
    "Nifty Midcap 150": "ind_niftymidcap150list.csv",
    "Nifty Smallcap 250": "ind_niftysmallcap250list.csv",
}

NIFTY_MOMENTUM_URL = (
    "https://www.niftyindices.com/IndexConstituent/"
    "ind_nifty500Momentum50_list.csv"
)
BSE_SENSEX_URL = (
    "https://www.bseindices.com/AsiaIndexAPI/api/"
    "Codewise_Indices/w?code=16"
)
BSE_500_URL = (
    "https://www.bseindices.com/AsiaIndexAPI/api/"
    "Codewise_Indices/w?code=17"
)

# BSE publishes SENSEX members by BSE scrip code and company name, while the
# application trades the corresponding NSE symbol.  These are stable symbol
# mappings for the current SENSEX constituent names; unmatched names are
# reported instead of being guessed.
BSE_SENSEX_SYMBOLS = {
    "ADANI PORTS AND SPECIAL ECONOM": "ADANIPORTS",
    "ASIAN PAINTS LTD.": "ASIANPAINT",
    "AXIS BANK LTD.": "AXISBANK",
    "BAJAJ FINANCE LIMITED": "BAJFINANCE",
    "BAJAJ FINSERV LTD.": "BAJAJFINSV",
    "BHARAT ELECTRONICS LTD.": "BEL",
    "BHARTI AIRTEL LTD.": "BHARTIARTL",
    "Eternal Limited": "ETERNAL",
    "HCL TECHNOLOGIES LTD.": "HCLTECH",
    "HDFC BANK LTD.": "HDFCBANK",
    "HINDUSTAN UNILEVER LTD.": "HINDUNILVR",
    "ICICI BANK LTD.": "ICICIBANK",
    "INFOSYS LTD.": "INFY",
    "InterGlobe Aviation Limited": "INDIGO",
    "ITC LTD.": "ITC",
    "KOTAK MAHINDRA BANK LTD.": "KOTAKBANK",
    "LARSEN & TOUBRO LTD.": "LT",
    "MAHINDRA & MAHINDRA LTD.": "M&M",
    "MARUTI SUZUKI INDIA LTD.": "MARUTI",
    "NTPC LTD.": "NTPC",
    "POWER GRID CORPORATION OF INDI": "POWERGRID",
    "RELIANCE INDUSTRIES LTD.": "RELIANCE",
    "STATE BANK OF INDIA": "SBIN",
    "SUN PHARMACEUTICAL INDUSTRIES": "SUNPHARMA",
    "TATA CONSULTANCY SERVICES LTD.": "TCS",
    "TATA STEEL LTD.": "TATASTEEL",
    "TECH MAHINDRA LTD.": "TECHM",
    "TITAN COMPANY LIMITED": "TITAN",
    "TRENT LTD.": "TRENT",
    "ULTRATECH CEMENT LTD.": "ULTRACEMCO",
}


def _fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.nseindia.com/",
            "Accept": "text/csv,application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def _nse_symbols(payload: bytes) -> list[str]:
    rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    return list(dict.fromkeys(row["Symbol"].strip().upper() for row in rows if row.get("Symbol")))


def _bse_symbols(payload: bytes, instrument_symbols: dict[str, str]) -> list[str]:
    data = json.loads(payload.decode("utf-8"))
    symbols = []
    for row in data.get("Table", []):
        name = (row.get("SCRIPNAME") or "").strip()
        symbol = BSE_SENSEX_SYMBOLS.get(name) or instrument_symbols.get(
            str(row.get("SCRIP_CODE", ""))
        )
        if symbol:
            symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def _sync_membership(db: Database, watchlist_name: str, symbols: list[str], stock_by_name: dict[str, dict]) -> tuple[int, list[str]]:
    watchlist = db.fetch_one(
        """
        SELECT id
        FROM watchlists
        WHERE is_active = TRUE
          AND list_type = 'index'
          AND (name = %s OR description ILIKE %s)
        ORDER BY CASE WHEN name = %s THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (watchlist_name, f"{watchlist_name} —%", watchlist_name),
    )
    if not watchlist:
        return 0, symbols

    matched = [stock_by_name[symbol] for symbol in symbols if symbol in stock_by_name]
    missing = [symbol for symbol in symbols if symbol not in stock_by_name]

    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM watchlist_items WHERE watchlist_id = %s AND source = %s",
            (watchlist["id"], "index_sync"),
        )
        for priority, stock in enumerate(matched, start=len(matched)):
            cur.execute(
                """
                INSERT INTO watchlist_items
                    (watchlist_id, stock_id, source, priority, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (watchlist_id, stock_id) DO NOTHING
                """,
                (
                    watchlist["id"],
                    stock["id"],
                    "index_sync",
                    priority,
                    "Constituent synced from the official index source",
                ),
            )
    return len(matched), missing


def main() -> int:
    settings = Settings()
    db = Database(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_connections=1,
        max_connections=2,
    )
    db.connect()
    try:
        stocks = db.fetch_all(
            """
            SELECT id, name, symbol
            FROM stocks
            WHERE exchange = 'NSE' AND is_active = TRUE
              AND name NOT LIKE '%%NSETEST%%'
            """
        )
        stock_by_name = {row["name"].upper(): row for row in stocks}

        # SmartAPI's local instrument master lets BSE scrip codes resolve to
        # the same NSE ticker used by the stocks table.  This is used for the
        # BSE 500 list and also covers new Sensex members without hardcoding
        # each company name.
        instrument_symbols: dict[str, str] = {}
        instrument_files = sorted(
            (PROJECT_ROOT / "data_cache" / "instruments").glob("instruments_*.json"),
            reverse=True,
        )
        if instrument_files:
            with instrument_files[0].open(encoding="utf-8") as handle:
                instruments = json.load(handle)
            instrument_symbols = {
                str(row["token"]): str(row["name"]).upper()
                for row in instruments
                if row.get("exch_seg") == "BSE"
                and not row.get("expiry")
                and row.get("instrumenttype", "") == ""
                and row.get("token")
                and row.get("name")
            }

        sources: dict[str, tuple[str, str]] = {
            name: ("NSE", NSE_ARCHIVE + filename)
            for name, filename in NSE_SOURCES.items()
        }
        sources["Nifty500 Momentum 50"] = ("NSE", NIFTY_MOMENTUM_URL)
        sources["S&P BSE Sensex"] = ("BSE", BSE_SENSEX_URL)
        sources["S&P BSE 500"] = ("BSE", BSE_500_URL)

        failed = False
        for name, (exchange, url) in sources.items():
            try:
                payload = _fetch(url)
                symbols = (
                    _nse_symbols(payload)
                    if exchange == "NSE"
                    else _bse_symbols(payload, instrument_symbols)
                )
                matched, missing = _sync_membership(db, name, symbols, stock_by_name)
                print(f"{name}: {matched}/{len(symbols)} constituents synced")
                if missing:
                    print(f"  unmatched: {', '.join(missing)}")
            except Exception as exc:
                failed = True
                print(f"{name}: failed — {exc}")

        return 1 if failed else 0
    finally:
        db.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
