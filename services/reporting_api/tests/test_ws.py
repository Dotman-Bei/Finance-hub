"""Tests for the live-notification relay (build.md Sec. 12).

The behaviour that matters most here is negative: when the event bus is down
the relay must stay silent. A dashboard showing invented activity is worse than
one showing none, because a quiet feed is visibly quiet while a fabricated one
is indistinguishable from real work.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from services.reporting_api.app.ws import ConnectionManager, EventRelay
from shared.events import EVENT_CHANNEL, EventPublisher, EventType, exception_created


class FakeSocket:
    """Records what would have been sent to a browser."""

    def __init__(self, fail_after: int | None = None):
        self.sent: list[dict] = []
        self.accepted = False
        self.fail_after = fail_after

    async def accept(self):
        self.accepted = True

    async def send_text(self, payload: str):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionResetError("peer went away")
        self.sent.append(json.loads(payload))


# ── Connection manager ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_reaches_every_connection():
    manager = ConnectionManager()
    a, b = FakeSocket(), FakeSocket()
    await manager.connect(a)
    await manager.connect(b)

    await manager.broadcast({"type": "exception.created", "payload": {"id": "x"}})

    assert a.sent == b.sent
    assert a.sent[0]["type"] == "exception.created"
    assert manager.live == 2


@pytest.mark.asyncio
async def test_dead_connections_are_reaped_not_retried():
    manager = ConnectionManager()
    healthy, dead = FakeSocket(), FakeSocket(fail_after=0)
    await manager.connect(healthy)
    await manager.connect(dead)

    await manager.broadcast({"type": "heartbeat", "payload": {}})

    assert manager.live == 1
    assert len(healthy.sent) == 1


@pytest.mark.asyncio
async def test_broadcast_with_no_listeners_is_a_no_op():
    manager = ConnectionManager()
    await manager.broadcast({"type": "exception.created", "payload": {}})
    assert manager.live == 0


@pytest.mark.asyncio
async def test_disconnect_removes_the_connection():
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket)
    await manager.disconnect(socket)
    assert manager.live == 0


# ── Relay ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_forwards_a_published_event():
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket)

    relay = EventRelay(manager, url="redis://unused")
    relay._loop = asyncio.get_running_loop()
    pump = asyncio.create_task(relay._drain())

    # Exactly the bytes a publisher would put on the channel.
    relay._offer(
        json.dumps({"type": EventType.EXCEPTION_CREATED, "payload": {"id": "abc"}})
    )
    await asyncio.sleep(0.05)

    pump.cancel()
    assert socket.sent[0]["type"] == EventType.EXCEPTION_CREATED
    assert relay.events_relayed == 1


@pytest.mark.asyncio
async def test_relay_discards_unparseable_messages():
    """A malformed message must not take the relay down."""
    manager = ConnectionManager()
    relay = EventRelay(manager, url="redis://unused")
    relay._loop = asyncio.get_running_loop()

    relay._offer("{not json")
    relay._offer(json.dumps(["an", "array"]))
    relay._offer(json.dumps({"no_type_field": True}))
    await asyncio.sleep(0.02)

    assert relay._queue.qsize() == 0


@pytest.mark.asyncio
async def test_relay_emits_nothing_when_the_bus_is_down():
    """The no-fabrication guarantee. A relay with no Redis has an empty queue
    and broadcasts nothing - it never invents activity to look alive."""
    manager = ConnectionManager()
    socket = FakeSocket()
    await manager.connect(socket)

    relay = EventRelay(manager, url="redis://127.0.0.1:1/0")  # nothing listening
    relay._loop = asyncio.get_running_loop()
    pump = asyncio.create_task(relay._drain())

    await asyncio.sleep(0.1)
    pump.cancel()

    assert socket.sent == []
    assert relay.events_relayed == 0
    assert relay.connected is False


@pytest.mark.asyncio
async def test_queue_overflow_drops_rather_than_blocking():
    """A browser that stops reading must not stall the relay for everyone."""
    manager = ConnectionManager()
    relay = EventRelay(manager, url="redis://unused")
    relay._loop = asyncio.get_running_loop()

    for i in range(relay._queue.maxsize + 25):
        relay._offer(json.dumps({"type": "exception.created", "payload": {"i": i}}))
    await asyncio.sleep(0.05)

    assert relay.events_dropped > 0
    assert relay._queue.qsize() <= relay._queue.maxsize


def test_relay_stats_report_the_truth():
    relay = EventRelay(ConnectionManager(), url="redis://unused")
    stats = relay.stats()
    assert stats["redis_connected"] is False
    assert stats["events_relayed"] == 0
    assert stats["channel"] == EVENT_CHANNEL


# ── Publisher ────────────────────────────────────────────────────────────


def test_publish_without_redis_reports_failure_not_success():
    publisher = EventPublisher(url="redis://127.0.0.1:1/0")
    assert publisher.publish(EventType.EXCEPTION_CREATED, {"id": "x"}) is False
    assert publisher.stats()["dropped"] == 1
    assert publisher.stats()["published"] == 0


def test_exception_created_payload_matches_the_dashboard_shape():
    """`normalizeException` in the frontend expects a nested transaction."""
    payload = exception_created(
        "exc-1",
        {
            "id": "txn-1",
            "external_id": "TXN-9",
            "source_type": "bank_api",
            "amount": 1250.5,
            "currency": "USD",
            "txn_date": "2026-01-05",
            "description": "Meridian Capital Ltd - settlement",
            "reference_code": "REF-12345",
        },
        reason="below confidence threshold",
        best_confidence=0.81,
    )

    assert payload["state"] == "OPEN"
    assert payload["category"] is None          # Subsystem 3 classifies later
    assert payload["transaction"]["amount"] == 1250.5
    assert payload["transaction"]["external_id"] == "TXN-9"
    assert payload["matching_engine"]["best_confidence"] == 0.81
