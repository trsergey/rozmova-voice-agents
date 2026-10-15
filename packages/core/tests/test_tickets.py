import time
from uuid import uuid4

import pytest
from fakeredis import FakeAsyncRedis

from rozmova_core import tickets
from rozmova_core.tickets import Ticket, TicketError

SECRET = "test-secret"  # noqa: S105 - a test-only HMAC key


def make_ticket(**overrides: object) -> Ticket:
    fields: dict[str, object] = {
        "user_id": "viewer-1",
        "bot_id": uuid4(),
        "call_id": uuid4(),
        "debug": True,
        "exp": int(time.time()) + 60,
    }
    return Ticket.model_validate(fields | overrides)


def test_sign_and_verify_round_trip() -> None:
    ticket = make_ticket()

    assert tickets.verify(tickets.sign(ticket, SECRET), SECRET) == ticket


def test_token_is_url_safe() -> None:
    token = tickets.sign(make_ticket(user_id="ü/+?&="), SECRET)

    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")


def test_wrong_secret_is_rejected() -> None:
    token = tickets.sign(make_ticket(), SECRET)

    with pytest.raises(TicketError, match="bad signature"):
        tickets.verify(token, "other-secret")


def test_tampered_payload_is_rejected() -> None:
    ticket = make_ticket(debug=False)
    _, mac = tickets.sign(ticket, SECRET).split(".")
    forged, _ = tickets.sign(ticket.model_copy(update={"debug": True}), SECRET).split(".")

    with pytest.raises(TicketError, match="bad signature"):
        tickets.verify(f"{forged}.{mac}", SECRET)


@pytest.mark.parametrize("token", ["", "no-dot", ".", "abc.def", "a.b.c"])
def test_malformed_token_is_rejected(token: str) -> None:
    with pytest.raises(TicketError):
        tickets.verify(token, SECRET)


def test_signed_garbage_payload_is_rejected() -> None:
    payload = tickets._b64encode(b'{"user_id": 1}')
    token = f"{payload}.{tickets._mac(payload, SECRET)}"

    with pytest.raises(TicketError, match="bad payload"):
        tickets.verify(token, SECRET)


def test_expired_ticket_is_rejected() -> None:
    token = tickets.sign(make_ticket(exp=1_000), SECRET)

    assert tickets.verify(token, SECRET, now=999).exp == 1_000
    with pytest.raises(TicketError, match="expired"):
        tickets.verify(token, SECRET, now=1_000)


async def test_issued_ticket_is_consumed_once() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    bot_id, call_id = uuid4(), uuid4()

    token = await tickets.issue(redis, SECRET, user_id="viewer-1", bot_id=bot_id, call_id=call_id)
    ticket = tickets.verify(token, SECRET)

    assert (ticket.user_id, ticket.bot_id, ticket.call_id, ticket.debug) == (
        "viewer-1",
        bot_id,
        call_id,
        False,
    )
    assert 0 < await redis.ttl(f"ticket:{ticket.id}") <= tickets.TICKET_TTL_SECONDS
    assert await tickets.consume(redis, ticket.id) is True
    assert await tickets.consume(redis, ticket.id) is False
    await redis.aclose()


async def test_unknown_ticket_is_not_consumed() -> None:
    redis = FakeAsyncRedis(decode_responses=True)

    assert await tickets.consume(redis, uuid4()) is False
    await redis.aclose()
