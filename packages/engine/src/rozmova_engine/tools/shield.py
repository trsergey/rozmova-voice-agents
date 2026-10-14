"""Finishing side-effect tools when the turn is cancelled (SDD "Діалоговий гейтвей").

A subscriber may interrupt while ``activate_service`` is running. Cancelling the call
halfway could leave the service activated while the bot never learns it, so the call runs
as its own task under ``asyncio.shield``: the turn's cancellation does not reach it, the
caller waits for it to finish (the tool timeout bounds the wait), records the real result,
and only then lets the cancellation continue.
"""

import asyncio
from collections.abc import Awaitable


async def complete_despite_cancel[T](work: Awaitable[T]) -> tuple[T, bool]:
    """Await ``work`` to the end even if the caller is cancelled meanwhile.

    Returns the result and whether a cancellation arrived. A caller that gets ``True``
    must record the result and then raise ``asyncio.CancelledError`` itself.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                # The work itself was cancelled (e.g. the event loop is shutting down).
                raise
            cancelled = True
