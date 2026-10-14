import asyncio

import pytest

from gateway.outbox import Outbox, OutboxStalledError
from rozmova_contracts import protocol


def delta(text: str, turn: int = 2) -> protocol.AssistantDelta:
    return protocol.AssistantDelta(turn_id=turn, text=text)


async def drain(outbox: Outbox) -> list[protocol.ProtocolMessage]:
    events = []
    while len(outbox):
        events.append(await outbox.peek())
        await outbox.pop()
    return events


async def test_events_come_out_in_order() -> None:
    outbox = Outbox()
    for text in ("a", "b", "c"):
        await outbox.put(delta(text))
    assert await drain(outbox) == [delta("a"), delta("b"), delta("c")]


async def test_peek_keeps_the_event_until_pop() -> None:
    outbox = Outbox()
    await outbox.put(delta("a"))
    assert await outbox.peek() == delta("a")
    assert len(outbox) == 1
    await outbox.pop()
    assert len(outbox) == 0


async def test_full_outbox_coalesces_deltas_of_the_same_turn() -> None:
    outbox = Outbox(maxsize=3)
    for text in ("Ва", "ш ", "ба", "ла", "нс"):
        await outbox.put(delta(text))

    assert await drain(outbox) == [delta("Ва"), delta("ш "), delta("баланс")]


async def test_deltas_of_another_turn_are_not_merged() -> None:
    outbox = Outbox(maxsize=2, stall_timeout=0.01)
    await outbox.put(delta("a", turn=2))
    await outbox.put(delta("b", turn=2))
    with pytest.raises(OutboxStalledError):
        await outbox.put(delta("c", turn=3))


async def test_head_being_sent_is_never_merged_into() -> None:
    outbox = Outbox(maxsize=2, stall_timeout=0.01)
    await outbox.put(delta("a"))
    await outbox.put(protocol.TurnEnd(turn_id=2, timings=protocol.TurnTimings(total_ms=1)))
    with pytest.raises(OutboxStalledError):
        await outbox.put(delta("b"))


async def test_other_events_wait_for_room() -> None:
    outbox = Outbox(maxsize=2)
    await outbox.put(delta("a"))
    await outbox.put(delta("b"))
    turn_end = protocol.TurnEnd(turn_id=2, timings=protocol.TurnTimings(total_ms=1))

    waiting = asyncio.create_task(outbox.put(turn_end))
    await asyncio.sleep(0)
    assert not waiting.done()
    await outbox.pop()
    await waiting

    assert await drain(outbox) == [delta("b"), turn_end]


async def test_stalled_client_raises_after_the_timeout() -> None:
    outbox = Outbox(maxsize=2, stall_timeout=0.01)
    await outbox.put(delta("a"))
    await outbox.put(delta("b"))
    with pytest.raises(OutboxStalledError):
        await outbox.put(protocol.SessionEnd(reason="completed"))


async def test_take_all_empties_the_queue() -> None:
    outbox = Outbox()
    await outbox.put(delta("a"))
    await outbox.put(delta("b"))
    assert outbox.take_all() == [delta("a"), delta("b")]
    assert len(outbox) == 0


def test_maxsize_below_two_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        Outbox(maxsize=1)
