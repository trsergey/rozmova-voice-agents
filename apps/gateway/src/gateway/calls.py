"""The open calls of this gateway process, and ending them all for a deploy (FR-21).

On a deploy Railway starts the new instance, switches traffic to it and sends the old one
SIGTERM; it kills the old one ``RAILWAY_DEPLOYMENT_DRAINING_SECONDS`` (60) later.
``drain`` uses that window: new calls get HTTP 503, every open session finishes its
running turn and ends with ``session_end{shutdown}``; its state is already in Redis, so
the client reconnects to the new instance with the same call_id and goes on.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

from gateway.session import FLUSH_TIMEOUT, Session

log = structlog.get_logger(__name__)


class Calls:
    def __init__(self) -> None:
        self.draining = False
        self._deadline = 0.0
        self._open: set[Session] = set()
        self._idle = asyncio.Event()
        self._idle.set()

    def __len__(self) -> int:
        return len(self._open)

    @contextmanager
    def track(self, session: Session) -> Iterator[None]:
        """Count ``session`` as open while the block runs."""
        self._open.add(session)
        self._idle.clear()
        if self.draining:
            # Admitted just before the drain began: end it with the others.
            session.drain(self._deadline)
        try:
            yield
        finally:
            self._open.discard(session)
            if not self._open:
                self._idle.set()

    async def drain(self, seconds: float) -> None:
        """Refuse new calls and end the open ones; running turns get ``seconds`` to finish.

        Returns when every session has closed, or ``FLUSH_TIMEOUT`` after the deadline at
        the latest; a session still open then is left to the server's own shutdown.
        """
        self.draining = True
        self._deadline = asyncio.get_running_loop().time() + seconds
        log.info("drain_started", calls=len(self._open), seconds=seconds)
        for session in self._open:
            session.drain(self._deadline)
        try:
            async with asyncio.timeout(seconds + FLUSH_TIMEOUT):
                await self._idle.wait()
        except TimeoutError:
            log.warning("drain_incomplete", calls=len(self._open))
            return
        log.info("drain_finished")
