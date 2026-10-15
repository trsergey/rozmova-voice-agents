"""Errors the admin API returns to clients: ``{"error": {"code": ..., "message": ...}}``.

``code`` is stable and meant for the SPA to branch on; ``message`` is for people; optional
``details`` carry structured data (e.g. validation issues with ``node_id``).
"""

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


async def _handle(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(ApiError, exc)  # registered for ApiError only
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        error["details"] = exc.details
    return JSONResponse({"error": error}, status_code=exc.status)


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle)
