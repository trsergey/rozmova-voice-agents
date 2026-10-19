"""Errors the admin API returns to clients: ``{"error": {"code": ..., "message": ...}}``.

``code`` is stable and meant for the SPA to branch on; ``message`` is for people; optional
``details`` carry structured data (e.g. validation issues with ``node_id``).

Request validation errors keep FastAPI's ``{"detail": [...]}`` shape (the OpenAPI schema
documents it) but drop ``input`` and ``ctx``: they echo what the client sent, and that may
be a password or a provider key.
"""

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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


async def _handle_validation(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)  # registered for RequestValidationError only
    issues = [
        {key: value for key, value in issue.items() if key not in {"input", "ctx"}}
        for issue in exc.errors()
    ]
    return JSONResponse({"detail": jsonable_encoder(issues)}, status_code=422)


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle)
    app.add_exception_handler(RequestValidationError, _handle_validation)
