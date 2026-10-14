"""Redis asyncio client factory."""

from redis.asyncio import Redis

from rozmova_core.settings import Settings


def create_redis(settings: Settings) -> Redis:
    """Create a pooled client; close it with ``await client.aclose()`` on shutdown."""
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        max_connections=settings.redis_max_connections,
        health_check_interval=30,
    )
