"""Workspace-wide pytest setup for integration tests (testcontainers)."""

import os
import sys
from functools import cache

import pytest

# Docker Desktop on macOS cannot bind-mount the per-user socket (~/.docker/run/docker.sock)
# into the testcontainers reaper; the socket path inside the Docker VM works.
if sys.platform == "darwin":
    os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")


@cache
def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]

        docker.from_env().ping()
    except Exception:
        return False
    return True


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("integration") and not _docker_available():
        pytest.skip("Docker is not available")
