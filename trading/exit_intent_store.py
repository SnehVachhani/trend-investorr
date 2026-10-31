"""
Durable exit intents for the live square-off.

Problem: the broker may accept an exit order and only list it in its order
book a moment later. A process that restarts in that window (or a second
instance) reads a book that does not show the exit, and would send another
one. In-memory state cannot bridge that gap; this store can.

Model: before calling placeOrder, square-off *claims* an intent keyed by
trading day + instrument + exit side. The claim is one file, created with
O_CREAT|O_EXCL, so of two processes sharing the directory exactly one wins.
After the broker acknowledges, the record gets the broker's order ids. The
intent stays unresolved until the broker lists the order and it reaches a
terminal state, or the position is flat — only then may a new exit for the
same key be claimed.

Layout (``root`` is ``settings.data_dir / "square_off_intents"``):

    root/<day>_<exchange>_<token>_<side>.json     unresolved intent
    root/resolved/<same>.<timestamp>.json         history, moved here on resolve

Every operation raises ExitIntentStoreError when the store cannot be read
or written; callers must treat that as "do not place an exit".
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from core.constants import IST


class ExitIntentStoreError(Exception):
    """The intent store could not be read or written."""


def intent_key(day: date, exchange: str, token: str, side: str) -> str:
    return f"{day:%Y%m%d}_{exchange.strip().upper()}_{token.strip()}_{side.strip().upper()}"


def order_tag(day: date, token: str, side: str) -> str:
    """Short tag for the broker order (SmartAPI ``ordertag``), e.g. SQ0304S2885."""
    return f"SQ{day:%m%d}{side.strip().upper()[0]}{token.strip()}"


@dataclass
class ExitIntent:
    key: str
    day: str                      # YYYYMMDD
    exchange: str
    token: str
    side: str                     # SELL / BUY
    trading_symbol: str
    quantity: int
    order_tag: str
    created_at: str = field(default_factory=lambda: datetime.now(IST).isoformat())
    owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")
    status: str = "intended"      # intended → placed
    broker_order_id: str = ""
    unique_order_id: str = ""
    placed_at: str = ""
    resolved_reason: str = ""
    resolved_at: str = ""

    @property
    def acknowledged(self) -> bool:
        return bool(self.broker_order_id)


class ExitIntentStore:
    def __init__(self, root: Path):
        self._root = Path(root)
        self._resolved = self._root / "resolved"

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: str) -> Path:
        return self._root / f"{key}.json"

    # ── reads ──

    def get(self, key: str) -> ExitIntent | None:
        """The unresolved intent for this key, or None."""
        path = self.path_for(key)
        try:
            if not path.exists():
                return None
            return self._load(path)
        except ExitIntentStoreError:
            raise
        except OSError as e:
            raise ExitIntentStoreError(f"cannot read {path}: {e}") from e

    def unresolved_for_day(self, day: date) -> list[ExitIntent]:
        prefix = f"{day:%Y%m%d}_"
        try:
            if not self._root.exists():
                return []
            if not self._root.is_dir():
                raise ExitIntentStoreError(f"{self._root} is not a directory")
            paths = sorted(p for p in self._root.glob("*.json") if p.name.startswith(prefix))
            return [self._load(p) for p in paths]
        except OSError as e:
            raise ExitIntentStoreError(f"cannot list {self._root}: {e}") from e

    # ── writes ──

    def claim(self, intent: ExitIntent) -> bool:
        """
        Record the intent atomically. True if this caller created it; False
        if an unresolved intent with the same key already exists (another
        call or instance got there first).
        """
        path = self.path_for(intent.key)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        except OSError as e:
            raise ExitIntentStoreError(f"cannot create {path}: {e}") from e
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(intent), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            # Do not leave a half-written claim behind.
            try:
                path.unlink()
            except OSError:
                pass
            raise ExitIntentStoreError(f"cannot write {path}: {e}") from e
        return True

    def mark_placed(self, key: str, broker_order_id: str, unique_order_id: str = "") -> ExitIntent:
        intent = self._require(key)
        intent.status = "placed"
        intent.broker_order_id = str(broker_order_id)
        intent.unique_order_id = str(unique_order_id or "")
        intent.placed_at = datetime.now(IST).isoformat()
        self._rewrite(intent)
        return intent

    def resolve(self, key: str, reason: str) -> None:
        """The intent is finished (order terminal, or position flat): move it to history."""
        intent = self.get(key)
        if intent is None:
            return
        intent.resolved_reason = reason
        intent.resolved_at = datetime.now(IST).isoformat()
        src = self.path_for(key)
        dst = self._resolved / f"{key}.{datetime.now(IST):%Y%m%dT%H%M%S%f}.json"
        try:
            self._resolved.mkdir(parents=True, exist_ok=True)
            self._rewrite(intent)
            os.replace(src, dst)
        except OSError as e:
            raise ExitIntentStoreError(f"cannot resolve {src}: {e}") from e

    # ── internals ──

    def _require(self, key: str) -> ExitIntent:
        intent = self.get(key)
        if intent is None:
            raise ExitIntentStoreError(f"no unresolved intent for {key}")
        return intent

    def _load(self, path: Path) -> ExitIntent:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ExitIntent(**data)
        except (OSError, ValueError, TypeError) as e:
            raise ExitIntentStoreError(f"unreadable intent record {path}: {e}") from e

    def _rewrite(self, intent: ExitIntent) -> None:
        path = self.path_for(intent.key)
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(intent), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            raise ExitIntentStoreError(f"cannot update {path}: {e}") from e
