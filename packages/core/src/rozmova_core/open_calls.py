"""Calls open right now, in Redis: ``ratelimit:{user_id}:calls``, one sorted set per caller.

The gateway adds a member (the connection id, scored by its start time) when a call opens
and removes it when the call closes; it also caps the size of the set (calls per user).
A member left by a crashed gateway goes stale after the longest possible call. admin-api's
"Операції" page counts the fresh members across all callers.
"""

PATTERN = "ratelimit:*:calls"


def key(user_id: str) -> str:
    return f"ratelimit:{user_id}:calls"
