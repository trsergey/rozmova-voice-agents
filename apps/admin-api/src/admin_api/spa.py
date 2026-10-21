"""Serves the built admin-web from ``admin_web_dir``: one origin with the API, so the
session cookie needs no CORS (SDD "Admin API і адмінка").

The API owns the root paths (``/bots``, ``/calls``...), so the SPA's pages live under
``/admin/`` and ``/demo/`` (ROZ-68). Any path under them returns ``index.html`` and the
client router picks the page (history fallback); the hashed bundle is under ``/assets/``.

Caching (ROZ-95): the HTML is ``no-store``, because ``no-cache`` does not stop Chrome from
showing a cached page on back/forward, and that old page asks for chunks a newer deploy no
longer has. The hashed assets never change, so they are cached for a year. A missing asset
is a 404, never the page: the client turns a missing chunk into one reload.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

PAGE_PREFIXES = ("/admin", "/demo")
PAGE_CACHE = "no-store"
ASSET_CACHE = "public, max-age=31536000, immutable"


class HashedAssets(StaticFiles):
    """The build's ``/assets/``: file names carry a content hash, so they never change."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = ASSET_CACHE
        return response


def install(app: FastAPI, web_dir: Path) -> None:
    index = web_dir / "index.html"
    app.mount("/assets", HashedAssets(directory=web_dir / "assets"), name="assets")

    async def page() -> FileResponse:
        # The HTML names the hashed bundle of this deploy, so it must never be cached.
        return FileResponse(index, headers={"Cache-Control": PAGE_CACHE})

    for prefix in PAGE_PREFIXES:
        app.add_api_route(prefix, page, include_in_schema=False)
        app.add_api_route(prefix + "/{path:path}", page, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        # The public link: visitors with an access code land on the demo chat.
        return RedirectResponse("/demo/")
