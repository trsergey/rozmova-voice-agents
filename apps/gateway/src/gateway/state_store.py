"""Session state in Redis for reconnects (FR-21): ``session:{call_id}``, TTL 30 min.

One ``SessionStore`` per connection. The hash holds the ``SessionState`` fields plus
``owner``, the id of the connection that may write it, a fencing token:

- ``claim`` makes this connection the owner and reads the state in one atomic step;
- ``save`` and ``drop`` do nothing unless this connection is still the owner.

Without it a stale connection overwrites a newer one. A mobile client reconnects long
before the server notices the old socket is dead (ping timeout, 30 s); the old session
then ends and saves the state it had, losing the turns made over the new connection.
With the owner check that late save is refused and the old session learns it was
superseded. Each step is a Lua script, so no other client runs between check and write.
"""

from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis

from rozmova_engine.session import SessionState

SESSION_TTL_SECONDS = 30 * 60
OWNER = "owner"

# KEYS[1] = session key, ARGV[1] = owner. Returns HGETALL as a flat list, empty if no state.
_CLAIM = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {} end
redis.call('HSET', KEYS[1], 'owner', ARGV[1])
return redis.call('HGETALL', KEYS[1])
"""

# KEYS[1] = session key, ARGV[1] = owner, ARGV[2] = TTL, ARGV[3..] = field, value, ...
_SAVE = """
local owner = redis.call('HGET', KEYS[1], 'owner')
if owner and owner ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'owner', ARGV[1], unpack(ARGV, 3))
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

# KEYS[1] = session key, ARGV[1] = owner.
_DROP = """
if redis.call('HGET', KEYS[1], 'owner') == ARGV[1] then redis.call('DEL', KEYS[1]) end
return 0
"""


def session_key(call_id: UUID) -> str:
    return f"session:{call_id}"


class SessionStore:
    def __init__(self, redis: Redis, call_id: UUID, *, ttl: int = SESSION_TTL_SECONDS) -> None:
        self._key = session_key(call_id)
        self._ttl = ttl
        self.owner = uuid4().hex
        # EVALSHA with a fallback to EVAL: the script text is sent once per Redis server.
        self._claim = redis.register_script(_CLAIM)
        self._save = redis.register_script(_SAVE)
        self._drop = redis.register_script(_DROP)

    async def claim(self) -> SessionState | None:
        """Take over the call and return its state; ``None`` if there is none.

        Raises ``SessionStateError`` if the stored state cannot be read.
        """
        flat: list[Any] = await self._claim(keys=[self._key], args=[self.owner])
        if not flat:
            return None
        fields = dict(zip(flat[::2], flat[1::2], strict=True))
        fields.pop(OWNER, None)
        return SessionState.from_redis(fields)

    async def save(self, state: SessionState) -> bool:
        """Write the state and refresh the TTL; ``False`` if another connection owns it."""
        pairs = [item for field in state.to_redis().items() for item in field]
        saved = await self._save(keys=[self._key], args=[self.owner, self._ttl, *pairs])
        return bool(saved)

    async def drop(self) -> None:
        """Forget a finished call, unless another connection owns it by now."""
        await self._drop(keys=[self._key], args=[self.owner])
