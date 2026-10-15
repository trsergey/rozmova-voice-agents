"""FastAPI app of the admin API. For now only the health check the container needs; auth,
bots, versions and calls come with their tickets (ROZ-26 onwards)."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Rozmova admin API")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
