"""Laundry Management service — MongoDB + AES-256-GCM envelope encryption.

Follows the same architecture as bill_service: JWT auth from user-service,
three-database connection manager, and encrypted sensitive fields.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .config import settings
from .routers import routers
from .error_responses import NotFoundError, ForbiddenError, ConflictError, ValidationError, BadRequestError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("laundry-management")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from .database.main_db import ensure_indexes
        from .database.connection_manager import ping

        ok = await ping("main")
        if ok:
            await ensure_indexes()
            logger.info("MongoDB connected and indexes ensured")
        else:
            logger.warning("MongoDB not reachable at startup (will retry on demand)")
    except Exception as e:
        logger.warning(f"MongoDB init skipped: {e}")
    yield


app = FastAPI(title="Laundry Management & Historical Records", version="1.0.0", lifespan=lifespan)

from .security import apply_security

CACHEABLE_PATH_PREFIXES = ("/api/customers", "/api/items", "/api/salary/slips")


@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.method == "GET" and request.url.path.startswith(CACHEABLE_PATH_PREFIXES):
        response.headers["Cache-Control"] = "private, max-age=60"
    return response

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://lovelaundry-manager.vercel.app",
    "https://public.lovelaundry.lk",
]

origins = ALLOWED_ORIGINS if settings.cors_origins == ["*"] or not settings.cors_origins else settings.cors_origins
apply_security(app, rate_limit=300)


@app.get("/")
async def root():
    return {"service": "laundry-management", "status": "ok"}


@app.get("/api/health")
async def health():
    from .database.connection_manager import ping

    main_ok = await ping("main")
    return {
        "status": "ok" if main_ok else "degraded",
        "database": "main" if main_ok else "unreachable",
        "security": {
            "headers": True,
            "rate_limiting": True,
        },
    }


for r in routers:
    app.include_router(r, prefix="/api")