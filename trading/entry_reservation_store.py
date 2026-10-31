"""
Durable entry reservations for live BUY orders.

Problem: the broker may accept a BUY and only list it in its order book a
moment later; or the reply may be lost (timeout) after the broker recorded
the order. A repeated signal in that window — from this process or a fresh
one after a restart — reads books that show nothing and would buy again.
In-memory state cannot bridge that gap; this store can.

Model: before calling placeOrder, the order manager *reserves* the entry,
keyed by trading day + instrument + product type, recording the quantity
and the price estimate the order was sized with. The reservation is one
file created with O_CREAT|O_EXCL, so of two callers sharing the directory
exactly one wins. It is kept after an acknowledgement (with the broker's
ids), after a timeout and after an unclear reply, and any later check that
shares the directory recovers it: until the broker shows the order in a
terminal state, the reserved quantity is exposure — a missing broker row
is not proof of rejection. Only an explicit broker rejection releases it
immediately.

Every unresolved reservation — any instrument, any day — draws on the
same capital budget and position slots until it is resolved; the order
manager counts them all when it sizes a new entry, from the broker's books
where the order is already visible there and from the reservation where
it is not, never both.

Deciding an entry (read the broker's books, check limits, size, reserve)
must be ONE operation across processes, or two instances can both size
against the same balance. ``lock()`` is an exclusive ``flock`` on
``root/.lock`` for that purpose: held from the book read until the
reservation is written. The OS releases it if the holder dies.

The intraday square-off closes new entries for the rest of the day for
EVERY instance sharing the storage — including one that starts later —
by writing a per-day hold file (``close_entries``) under the same lock.
A BUY decision refuses the live leg while today's hold exists. The hold
is keyed by trading day, so it expires with the day and a same-day reset
cannot reopen it. Exits never consult it.

Layout (``root`` is ``settings.data_dir / "entry_reservations"``):

    root/.lock                                     cross-process decision lock
    root/entries_closed_<day>.json                 shared entry hold for that day
    root/<day>_<exchange>_<token>_<product>.json   unresolved reservation
    root/resolved/<same>.<timestamp>.json          history, moved on resolve

A reservation is released only when the broker shows its order terminal
AND the position book reflects whatever filled, or on an explicit
rejection. A change of trading day releases nothing: an unresolved
reservation from an earlier day blocks new entries in its instrument until
the broker confirms what became of it (or the operator removes the file).

Every operation raises EntryReservationStoreError when the store cannot be
read or written; callers must treat that as "do not submit the order".
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from core.constants import IST


class EntryReservationStoreError(Exception):
    """The reservation store could not be read or written."""


def _day_str(day: date | str) -> str:
    return day if isinstance(day, str) else f"{day:%Y%m%d}"


def reservation_key(day: date | str, exchange: str, token: str, product_type: str) -> str:
    return f"{_day_str(day)}_{exchange.strip().upper()}_{token.strip()}_{product_type.strip().upper()}"


def entry_order_tag(day: date | str, token: str) -> str:
    """Short tag for the broker order (SmartAPI ``ordertag``), e.g. EN0304B2885."""
    d = _day_str(day)
    return f"EN{d[4:6]}{d[6:8]}B{token.strip()}"


@dataclass
class EntryReservation:
    key: str
    day: str                      # YYYYMMDD
    exchange: str
    token: str
    trading_symbol: str
    product_type: str             # INTRADAY / DELIVERY
    side: str                     # BUY
    quantity: int                 # as sized and sent
    price_estimate: float         # the price the order was sized with (capital it commits)
    order_tag: str
    created_at: str = field(default_factory=lambda: datetime.now(IST).isoformat())
    owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")
    status: str = "reserved"      # reserved → placed | unclear
    broker_order_id: str = ""
    unique_order_id: str = ""
    placed_at: str = ""
    submission: str = ""          # unclear: what happened at submission (timeout, None reply, …)
    resolved_reason: str = ""
    resolved_at: str = ""

    @property
    def acknowledged(self) -> bool:
        return bool(self.broker_order_id)


@dataclass
class EntryHold:
    """New entries closed for one trading day, for every instance sharing the storage."""

    day: str                      # YYYYMMDD
    reason: str
    closed_at: str = field(default_factory=lambda: datetime.now(IST).isoformat())
    owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")


class _EntryLock:
    """Exclusive cross-process lock (flock); acquired on construction, released on exit."""

    def __init__(self, path: Path, timeout: float):
        self._fd: int | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise EntryReservationStoreError(f"cannot open entry lock {path}: {e}") from e
        # Bounded by attempts rather than a clock deadline: a patched or
        # stalled clock must not turn a 30s wait into an endless one.
        interval = 0.01
        attempts = max(1, int(timeout / interval))
        for attempt in range(attempts):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == attempts - 1:
                    os.close(fd)
                    raise EntryReservationStoreError(
                        f"could not obtain the entry lock {path} within {timeout:.1f}s — another "
                        f"instance is deciding an entry"
                    )
                time.sleep(interval)
            except OSError as e:
                os.close(fd)
                raise EntryReservationStoreError(f"cannot lock {path}: {e}") from e
        self._fd = fd

    def __enter__(self) -> "_EntryLock":
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class EntryReservationStore:
    def __init__(self, root: Path, lock_timeout: float = 30.0):
        self._root = Path(root)
        self._resolved = self._root / "resolved"
        self._lock_timeout = lock_timeout

    def lock(self, timeout: float | None = None) -> _EntryLock:
        """
        Acquire the cross-process entry-decision lock (blocking up to
        `timeout`, default the store's lock_timeout); use as a context
        manager. Raises EntryReservationStoreError if it cannot be obtained
        — a BUY decision must then not proceed; square-off proceeds without
        it and reports that it did.
        """
        return _EntryLock(self._root / ".lock", self._lock_timeout if timeout is None else timeout)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: str) -> Path:
        return self._root / f"{key}.json"

    def hold_path_for(self, day: date | str) -> Path:
        return self._root / f"entries_closed_{_day_str(day)}.json"

    # ── shared entry hold ──

    def close_entries(self, day: date | str, reason: str) -> EntryHold:
        """Close new entries for `day` for every instance sharing this store (idempotent)."""
        existing = self.entries_closed(day)
        if existing is not None:
            return existing
        hold = EntryHold(day=_day_str(day), reason=reason)
        path = self.hold_path_for(day)
        tmp = path.with_suffix(".json.tmp")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(hold), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            raise EntryReservationStoreError(f"cannot write entry hold {path}: {e}") from e
        return hold

    def entries_closed(self, day: date | str) -> EntryHold | None:
        """The shared hold for `day`, or None if entries are open."""
        path = self.hold_path_for(day)
        try:
            if not path.exists():
                return None
            return EntryHold(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as e:
            raise EntryReservationStoreError(f"unreadable entry hold {path}: {e}") from e

    # ── reads ──

    def get(self, key: str) -> EntryReservation | None:
        """The unresolved reservation for this key, or None."""
        path = self.path_for(key)
        try:
            if not path.exists():
                return None
            return self._load(path)
        except EntryReservationStoreError:
            raise
        except OSError as e:
            raise EntryReservationStoreError(f"cannot read {path}: {e}") from e

    def unresolved_for_day(self, day: date | str) -> list[EntryReservation]:
        prefix = f"{_day_str(day)}_"
        try:
            if not self._root.exists():
                return []
            if not self._root.is_dir():
                raise EntryReservationStoreError(f"{self._root} is not a directory")
            paths = sorted(p for p in self._root.glob("*.json") if p.name.startswith(prefix))
            return [self._load(p) for p in paths]
        except OSError as e:
            raise EntryReservationStoreError(f"cannot list {self._root}: {e}") from e

    def unresolved(self) -> list[EntryReservation]:
        """Every unresolved reservation — any instrument, any day — oldest first."""
        try:
            if not self._root.exists():
                return []
            if not self._root.is_dir():
                raise EntryReservationStoreError(f"{self._root} is not a directory")
            # reservation files are <YYYYMMDD>_…json; entries_closed_<day>.json is the hold
            return [self._load(p) for p in sorted(self._root.glob("*.json")) if p.name[:8].isdigit()]
        except OSError as e:
            raise EntryReservationStoreError(f"cannot list {self._root}: {e}") from e

    def unresolved_for_instrument(
        self, exchange: str, token: str, product_type: str
    ) -> list[EntryReservation]:
        """Every unresolved reservation for this instrument, from ANY day, oldest first."""
        suffix = f"_{exchange.strip().upper()}_{token.strip()}_{product_type.strip().upper()}.json"
        try:
            if not self._root.exists():
                return []
            if not self._root.is_dir():
                raise EntryReservationStoreError(f"{self._root} is not a directory")
            paths = sorted(p for p in self._root.glob("*.json") if p.name.endswith(suffix))
            return [self._load(p) for p in paths]
        except OSError as e:
            raise EntryReservationStoreError(f"cannot list {self._root}: {e}") from e

    # ── writes ──

    def reserve(self, reservation: EntryReservation) -> bool:
        """
        Record the reservation atomically. True if this caller created it;
        False if an unresolved reservation with the same key already exists
        (another call or instance got there first).
        """
        path = self.path_for(reservation.key)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            if path.is_file():
                return False
            raise EntryReservationStoreError(f"cannot create {path}: {self._root} is not a directory")
        except OSError as e:
            raise EntryReservationStoreError(f"cannot create {path}: {e}") from e
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(reservation), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            # Do not leave a half-written reservation behind.
            try:
                path.unlink()
            except OSError:
                pass
            raise EntryReservationStoreError(f"cannot write {path}: {e}") from e
        return True

    def mark_placed(self, key: str, broker_order_id: str, unique_order_id: str = "") -> EntryReservation:
        """The broker acknowledged the order (accepted into its book — not filled)."""
        reservation = self._require(key)
        reservation.status = "placed"
        reservation.broker_order_id = str(broker_order_id)
        reservation.unique_order_id = str(unique_order_id or "")
        reservation.placed_at = datetime.now(IST).isoformat()
        reservation.submission = "acknowledged"
        self._rewrite(reservation)
        return reservation

    def mark_unclear(self, key: str, detail: str) -> EntryReservation:
        """The submission's outcome is unknown (timeout, None reply, …): keep the reservation."""
        reservation = self._require(key)
        reservation.status = "unclear"
        reservation.submission = detail
        self._rewrite(reservation)
        return reservation

    def resolve(self, key: str, reason: str) -> None:
        """The reservation is finished (order terminal at the broker, or explicitly rejected)."""
        reservation = self.get(key)
        if reservation is None:
            return
        reservation.resolved_reason = reason
        reservation.resolved_at = datetime.now(IST).isoformat()
        src = self.path_for(key)
        dst = self._resolved / f"{key}.{datetime.now(IST):%Y%m%dT%H%M%S%f}.json"
        try:
            self._resolved.mkdir(parents=True, exist_ok=True)
            self._rewrite(reservation)
            os.replace(src, dst)
        except OSError as e:
            raise EntryReservationStoreError(f"cannot resolve {src}: {e}") from e

    # ── internals ──

    def _require(self, key: str) -> EntryReservation:
        reservation = self.get(key)
        if reservation is None:
            raise EntryReservationStoreError(f"no unresolved reservation for {key}")
        return reservation

    def _load(self, path: Path) -> EntryReservation:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return EntryReservation(**data)
        except (OSError, ValueError, TypeError) as e:
            raise EntryReservationStoreError(f"unreadable reservation record {path}: {e}") from e

    def _rewrite(self, reservation: EntryReservation) -> None:
        path = self.path_for(reservation.key)
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(reservation), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            raise EntryReservationStoreError(f"cannot update {path}: {e}") from e
