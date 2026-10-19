"""Serves the built admin-web from ``admin_web_dir``: one origin with the API, so the
session cookie needs no CORS (SDD "Admin API і адмінка").

The API owns the root paths (``/bots``, ``/calls``...), so the SPA's pages live under
``/admin/`` and ``/demo/`` (ROZ-68). Any path under them returns ``index.html`` and the
client router picks the page (history fallback); the hashed bundle is under ``/assets/``.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

PAGE_PREFIXES = ("/admin", "/demo")


def install(app: FastAPI, web_dir: Path) -> None:
    index = web_dir / "index.html"
    app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

    async def page() -> FileResponse:
        # The HTML names the hashed bundle of this deploy, so it must never be cached.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    for prefix in PAGE_PREFIXES:
        app.add_api_route(prefix, page, include_in_schema=False)
        app.add_api_route(prefix + "/{path:path}", page, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        # The public link: visitors with an access code land on the demo chat.
        return RedirectResponse("/demo/")
