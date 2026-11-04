from __future__ import annotations

import uuid

from fastapi import FastAPI, Request

from app.api.health import router as health_router
from app.api.v1.router import api_v1
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, request_id_ctx


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    prod = settings.app_env == "production"
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        rid = rid[:64]
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(api_v1)
    return app


app = create_app()
