"""Live exception notifications over WebSocket (build.md Sec. 12).

    "WS /ws/exceptions - push new exceptions live so teams don't manually
     refresh"

Bridges the Redis event channel (shared/events.py) to connected browsers. The
services that create work publish; this relays. It originates nothing.

That last point is the whole design constraint. If Redis is unreachable the
relay accepts connections, reports `degraded` once, and stays silent. It never
manufactures an event to make the dashboard look alive - a fabricated exception
is worse than a quiet one, because a quiet feed is visibly quiet while a
fabricated one is indistinguishable from real work.

Subscription runs in a worker thread rather than an async Redis client:
redis-py's blocking `listen()` is the reliable path, and one thread per process
relaying to an asyncio queue is simpler to reason about than a second event
loop. Broadcast fan-out stays on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import threading
from typing import Any

import redis
from fastapi import WebSocket, WebSocketDisconnect

from shared.config import settings
from shared.events import EVENT_CHANNEL

logger = logging.getLogger(__name__)

#: Dropped rather than queued indefinitely if a browser stops reading.
MAX_QUEUED_EVENTS = 500

#: Keeps intermediaries from closing an idle connection.
HEARTBEAT_SECONDS = 25


class ConnectionManager:
    """Tracks live sockets and fans events out to them."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.total_accepted = 0
        self.total_broadcast = 0

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
            self.total_accepted += 1
        logger.info("WebSocket connected (%d live)", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("WebSocket disconnected (%d live)", len(self._connections))

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._connections)

        if not targets:
            return

        payload = json.dumps(message, default=str)
        dead: list[WebSocket] = []

        for connection in targets:
            try:
                await connection.send_text(payload)
            except Exception:
                # A send failure means the peer is gone; reap rather than retry.
                dead.append(connection)

        if dead:
            async with self._lock:
                for connection in dead:
                    self._connections.discard(connection)

        self.total_broadcast += 1

    @property
    def live(self) -> int:
        return len(self._connections)


class EventRelay:
    """Subscribes to the Redis event channel and broadcasts what arrives."""

    def __init__(
        self,
        manager: ConnectionManager,
        url: str | None = None,
        channel: str = EVENT_CHANNEL,
    ) -> None:
        self.manager = manager
        self.url = url or settings.redis_url
        self.channel = channel

        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=MAX_QUEUED_EVENTS
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pump: asyncio.Task | None = None
        self._stop = threading.Event()

        self.connected = False
        self.events_relayed = 0
        self.events_dropped = 0
        self.last_error: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._pump = asyncio.create_task(self._drain())
        self._thread = threading.Thread(
            target=self._subscribe, name="event-relay", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.connected = False

    # ── redis side (worker thread) ───────────────────────────────────────

    def _subscribe(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                client = redis.Redis.from_url(
                    self.url, decode_responses=True, socket_connect_timeout=5
                )
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.channel)

                self.connected = True
                self.last_error = None
                backoff = 1.0
                logger.info("Relaying %s to WebSocket clients", self.channel)

                for message in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if message.get("type") != "message":
                        continue
                    self._offer(message.get("data"))

                pubsub.close()
                client.close()

            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                if not self._stop.is_set():
                    logger.warning(
                        "Event relay disconnected (%s); retrying in %.0fs. "
                        "Live notifications are paused - no events are "
                        "synthesised in the meantime.",
                        self.last_error, backoff,
                    )
                    self._stop.wait(backoff)
                    backoff = min(30.0, backoff * 2)

        self.connected = False

    def _offer(self, raw: Any) -> None:
        """Hand a message to the event loop. Called from the worker thread."""
        if self._loop is None or raw is None:
            return

        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.debug("Discarding unparseable event from %s", self.channel)
            return

        if not isinstance(event, dict) or "type" not in event:
            return

        def enqueue() -> None:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                # Backpressure: drop the newest rather than block the relay.
                # Postgres remains the durable record, so nothing is lost.
                self.events_dropped += 1

        self._loop.call_soon_threadsafe(enqueue)

    # ── event loop side ──────────────────────────────────────────────────

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.manager.broadcast(event)
                self.events_relayed += 1
            except Exception:
                logger.exception("Failed to broadcast an event")
            finally:
                self._queue.task_done()

    def stats(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "redis_connected": self.connected,
            "live_connections": self.manager.live,
            "events_relayed": self.events_relayed,
            "events_dropped": self.events_dropped,
            "queued": self._queue.qsize(),
            "last_error": self.last_error,
        }


async def serve(websocket: WebSocket, manager: ConnectionManager, relay: EventRelay) -> None:
    """Handle one `/ws/exceptions` connection.

    The opening frame states whether the feed is actually live, so a client
    can show a degraded indicator instead of assuming silence means calm.
    """
    await manager.connect(websocket)

    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "connection.established",
                    "payload": {
                        "channel": relay.channel,
                        "live": relay.connected,
                        # Explicit, so a quiet feed is never mistaken for a
                        # healthy one.
                        "detail": (
                            "Subscribed to the live event channel."
                            if relay.connected
                            else "Event bus unreachable - no events will arrive "
                            "until it recovers. Nothing is simulated."
                        ),
                    },
                    "emitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
        )

        while True:
            # Clients need send nothing; this both keeps the socket open and
            # detects a disconnect that never sent a close frame.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "heartbeat",
                            "payload": {"live": relay.connected},
                            "emitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                    )
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket closed unexpectedly", exc_info=True)
    finally:
        await manager.disconnect(websocket)


__all__ = ["ConnectionManager", "EventRelay", "serve", "HEARTBEAT_SECONDS"]
