"""Build the local symbol-to-company-name directory used by stock search.

Sources:
- NSE's public equity listing files (preferred because they contain the legal name).
- Zerodha's public NSE instrument dump (fallback for symbols missing from NSE files).

The generated directory is application metadata and does not modify Docker or the
PostgreSQL stocks table.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "data_cache" / "company_directory.json"
USER_AGENT = "Trend-Investorr company directory sync/1.0"

NSE_URLS = (
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equities/SME_EQUITY_L.csv",
)
ZERODHA_NSE_URL = "https://api.kite.trade/instruments/NSE"


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8-sig")


def normalize_symbol(value: str | None) -> str:
    return (value or "").strip().upper().removesuffix("-EQ").removesuffix("-SM").removesuffix("-BE").removesuffix("-BZ").removesuffix("-ST")


def add_entry(directory: dict[str, dict[str, object]], symbol: str | None, company_name: str | None, source: str) -> None:
    base_symbol = normalize_symbol(symbol)
    name = " ".join((company_name or "").split())
    if not base_symbol or not name or name.casefold() == base_symbol.casefold():
        return
    directory.setdefault(
        base_symbol,
        {
            "company_name": name,
            "aliases": [name],
            "source": source,
        },
    )


def load_nse_directory(directory: dict[str, dict[str, object]]) -> list[str]:
    loaded = []
    for url in NSE_URLS:
        text = fetch_text(url)
        rows = csv.DictReader(io.StringIO(text))
        count = 0
        for row in rows:
            add_entry(
                directory,
                row.get("SYMBOL", ""),
                row.get("NAME OF COMPANY", ""),
                "nse_equity_listing",
            )
            count += 1
        loaded.append(f"{url} ({count} rows)")
    return loaded


def load_zerodha_directory(directory: dict[str, dict[str, object]]) -> str:
    rows = csv.DictReader(io.StringIO(fetch_text(ZERODHA_NSE_URL)))
    count = 0
    for row in rows:
        if row.get("instrument_type") != "EQ" or row.get("segment") != "NSE":
            continue
        before = len(directory)
        add_entry(directory, row.get("tradingsymbol", ""), row.get("name", ""), "zerodha_nse_instruments")
        count += len(directory) - before
    return f"{ZERODHA_NSE_URL} ({count} fallback names)"


def main() -> None:
    directory: dict[str, dict[str, object]] = {}
    sources = load_nse_directory(directory)
    sources.append(load_zerodha_directory(directory))
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "stocks": dict(sorted(directory.items())),
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(directory)} company names to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
