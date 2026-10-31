"""
Shared guards for tests/data.

Every WebSocketManager that opens a socket starts a health monitor thread
bound to that socket generation; a test that opens sockets must stop the
manager. This guard makes a leaked run or monitor thread fail the test
that leaked it, instead of surfacing as a miscount in a later test.
"""

import threading

import pytest


def websocket_threads() -> list[str]:
    return sorted(t.name for t in threading.enumerate() if t.name.startswith("ws-"))


@pytest.fixture(autouse=True)
def _no_leaked_websocket_threads():
    before = websocket_threads()
    assert before == [], f"websocket threads leaked by an earlier test: {before}"
    yield
    after = websocket_threads()
    assert after == [], f"websocket threads left alive: {after}"
