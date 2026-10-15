"""Errors the admin API returns to clients: ``{"error": {"code": ..., "message": ...}}``.

``code`` is stable and meant for the SPA to branch on; ``message`` is for people.
"""

from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


async def _handle(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(ApiError, exc)  # registered for ApiError only
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
    )


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle)
