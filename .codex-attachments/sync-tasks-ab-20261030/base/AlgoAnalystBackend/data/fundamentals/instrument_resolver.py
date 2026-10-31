"""Resolve the project's stock symbols to Upstox ISINs.

Upstox fundamentals are keyed by ISIN.  The daily NSE instrument master is a
more reliable bridge than fuzzy company-name matching and is cached locally so
the mapping does not cost a fundamentals request per stock.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from core.constants import IST
from core.exceptions import DataFetchError


@dataclass(frozen=True)
class UpstoxInstrument:
    isin: str
    instrument_key: str
    trading_symbol: str
    name: str
    instrument_type: str = ""


class UpstoxInstrumentResolver:
    """Download/cache the NSE instrument master and resolve local symbols."""

    def __init__(
        self,
        cache_dir: Path,
        url: str = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ):
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._url = url
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._by_symbol: dict[str, UpstoxInstrument] = {}
        self._by_name: dict[str, UpstoxInstrument] = {}
        self._loaded = False

    @property
    def cache_file(self) -> Path:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return self._cache_dir / f"nse_instruments_{today}.json"

    def load(self, force_refresh: bool = False) -> int:
        if not force_refresh and self.cache_file.exists():
            try:
                payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
                return self._index(payload)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(f"Upstox instrument cache invalid; refreshing: {exc}")

        try:
            response = self._session.get(self._url, timeout=self._timeout)
            response.raise_for_status()
            payload = json.loads(gzip.decompress(response.content).decode("utf-8"))
            self.cache_file.write_text(json.dumps(payload), encoding="utf-8")
            return self._index(payload)
        except (requests.RequestException, OSError, ValueError, TypeError) as exc:
            stale = sorted(self._cache_dir.glob("nse_instruments_*.json"), reverse=True)
            for candidate in stale:
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    logger.warning(f"Using stale Upstox instrument cache: {candidate.name}")
                    return self._index(payload)
                except (OSError, ValueError, TypeError):
                    continue
            raise DataFetchError(
                f"Unable to load Upstox NSE instruments: {exc}",
                details={"url": self._url},
            ) from exc

    def resolve(self, symbol: str, company_name: str = "") -> UpstoxInstrument | None:
        if not self._loaded:
            self.load()
        normalized = self.normalize_symbol(symbol)
        result = self._by_symbol.get(normalized)
        if result:
            return result
        if company_name:
            return self._by_name.get(self.normalize_name(company_name))
        return None

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        value = (symbol or "").strip().upper()
        value = value.split("-")[0]
        return re.sub(r"[^A-Z0-9]", "", value)

    @staticmethod
    def normalize_name(name: str) -> str:
        value = (name or "").upper()
        value = re.sub(r"\b(LIMITED|LTD|PRIVATE|PVT|INDIA|CORPORATION|CORP)\b", "", value)
        return re.sub(r"[^A-Z0-9]", "", value)

    def _index(self, payload: Any) -> int:
        if not isinstance(payload, list):
            raise DataFetchError("Upstox NSE instrument payload is not a list")
        self._by_symbol.clear()
        self._by_name.clear()
        for item in payload:
            if not isinstance(item, dict) or item.get("segment") != "NSE_EQ":
                continue
            isin = str(item.get("isin") or "").strip().upper()
            trading_symbol = str(item.get("trading_symbol") or "").strip().upper()
            instrument_key = str(item.get("instrument_key") or "").strip()
            if not isin or not trading_symbol or not instrument_key:
                continue
            candidate = UpstoxInstrument(
                isin=isin,
                instrument_key=instrument_key,
                trading_symbol=trading_symbol,
                name=str(item.get("name") or "").strip(),
                instrument_type=str(item.get("instrument_type") or "").strip().upper(),
            )
            key = self.normalize_symbol(trading_symbol)
            previous = self._by_symbol.get(key)
            # Prefer the normal EQ listing when both EQ and BE are present.
            if previous is None or (previous.instrument_type != "EQ" and candidate.instrument_type == "EQ"):
                self._by_symbol[key] = candidate
            name_key = self.normalize_name(candidate.name)
            if name_key and name_key not in self._by_name:
                self._by_name[name_key] = candidate
        self._loaded = True
        return len(self._by_symbol)
