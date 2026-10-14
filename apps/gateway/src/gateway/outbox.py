"""Bounded outgoing event queue with delta coalescing (SDD "Backpressure").

A slow client must not make the gateway buffer without limit. When the queue is full, a
new ``assistant_delta`` is merged into a delta of the same turn already at the tail, so
streaming text keeps flowing in bigger pieces; any other event waits for room, and after
``stall_timeout`` the session is closed with code 1013 ("try again later").

``asyncio.Queue`` cannot change its last item, which coalescing needs, so this is a small
deque guarded by one ``asyncio.Condition``.
"""

import asyncio
from collections import deque

from rozmova_contracts import protocol

MAXSIZE = 256
STALL_TIMEOUT = 10.0


class OutboxStalledError(Exception):
    """The client has not read anything for ``stall_timeout`` while the queue was full."""


class Outbox:
    def __init__(self, maxsize: int = MAXSIZE, stall_timeout: float = STALL_TIMEOUT) -> None:
        if maxsize < 2:
            raise ValueError("maxsize must be at least 2")
        self._items: deque[protocol.ProtocolMessage] = deque()
        self._maxsize = maxsize
        self._stall_timeout = stall_timeout
        self._changed = asyncio.Condition()

    def __len__(self) -> int:
        return len(self._items)

    async def put(self, event: protocol.ProtocolMessage) -> None:
        async with self._changed:
            if len(self._items) >= self._maxsize and self._coalesce(event):
                return
            try:
                async with asyncio.timeout(self._stall_timeout):
                    await self._changed.wait_for(lambda: len(self._items) < self._maxsize)
            except TimeoutError:
                raise OutboxStalledError(
                    f"outbox full for {self._stall_timeout:g} s, client is not reading"
                ) from None
            self._items.append(event)
            self._changed.notify_all()

    async def peek(self) -> protocol.ProtocolMessage:
        """Wait for the oldest event without removing it; ``pop`` it once it is sent.

        Popping after the send keeps an event that was being sent when the session closed,
        so the final flush still delivers it.
        """
        async with self._changed:
            await self._changed.wait_for(lambda: len(self._items) > 0)
            return self._items[0]

    async def pop(self) -> None:
        async with self._changed:
            self._items.popleft()
            self._changed.notify_all()

    def take_all(self) -> list[protocol.ProtocolMessage]:
        """Everything still queued, for the final flush after the writer has stopped."""
        items = list(self._items)
        self._items.clear()
        return items

    def _coalesce(self, event: protocol.ProtocolMessage) -> bool:
        # The head may be on its way to the client (peeked, not yet popped): never touch it.
        tail = self._items[-1] if len(self._items) > 1 else None
        if (
            isinstance(event, protocol.AssistantDelta)
            and isinstance(tail, protocol.AssistantDelta)
            and tail.turn_id == event.turn_id
        ):
            merged = protocol.AssistantDelta(turn_id=event.turn_id, text=tail.text + event.text)
            self._items[-1] = merged
            return True
        return False
