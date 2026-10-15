"""``python -m gateway.devticket [bot_id]``: a ticket for local runs before the admin API.

Signs and stores a ticket with the local settings (``ROZMOVA_TICKET_SECRET``, Redis) and
prints the WebSocket URL to connect with, e.g. ``websocat "$(python -m gateway.devticket)"``.
The admin API's ``POST /calls/ticket`` (ROZ-61) replaces it for real clients.
"""

import asyncio
import sys
from uuid import UUID, uuid4

from rozmova_core import tickets
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings


async def main(bot_id: UUID, *, debug: bool = True, port: int = 8001) -> str:
    settings = Settings()
    if settings.ticket_secret is None:
        raise SystemExit("set ROZMOVA_TICKET_SECRET")
    call_id = uuid4()
    redis = create_redis(settings)
    try:
        token = await tickets.issue(
            redis,
            settings.ticket_secret.get_secret_value(),
            user_id="dev",
            bot_id=bot_id,
            call_id=call_id,
            debug=debug,
        )
    finally:
        await redis.aclose()
    return f"ws://localhost:{port}/ws/calls/{bot_id}?call_id={call_id}&ticket={token}"


if __name__ == "__main__":
    print(asyncio.run(main(UUID(sys.argv[1]) if len(sys.argv) > 1 else uuid4())))
