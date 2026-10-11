import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.pool import close_pool, create_pool, get_pool
from app.infra.broker import broker
from app.infra.redis_client import close_redis, create_redis, get_redis
from app.routes import admin, agent, auth, chat, documents, internal, tickets

log = logging.getLogger("policydesk.api")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        extra = getattr(record, "fields", None)
        if extra:
            base.update(extra)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, default=str)


def setup_logging() -> None:
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True


async def connect_broker_with_retry(attempts: int = 10) -> None:
    for i in range(attempts):
        try:
            await broker.connect()
            return
        except Exception as e:
            log.warning("rabbitmq not ready (%s), retry %d", e, i + 1)
            await asyncio.sleep(min(10, 1 + i))
    log.error("rabbitmq unavailable; uploads will fail until it is reachable")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging()
    await run_migrations()
    await create_pool()
    await create_redis()
    await connect_broker_with_retry()
    consumers = None
    if s.run_worker_in_process and broker.ready:
        from app.worker import Consumers

        consumers = Consumers()
        await consumers.start(broker)
    log.info("started", extra={"fields": {"instance": s.instance_id, "ai": s.ai_backend, "retriever": s.retriever, "agent": s.agent_mode}})
    yield
    if consumers:
        await consumers.stop()
    await broker.close()
    await close_redis()
    await close_pool()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="PolicyDesk API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Instance", "X-Request-Id", "Retry-After"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled", extra={"fields": {"request_id": rid, "path": request.url.path}})
            response = JSONResponse({"detail": "Internal error"}, status_code=500)
        ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Instance"] = s.instance_id
        response.headers["X-Request-Id"] = rid
        if request.url.path not in ("/api/health", "/api/ping"):
            user = getattr(request.state, "user", None)
            log.info(
                "request",
                extra={"fields": {"request_id": rid, "method": request.method, "path": request.url.path, "status": response.status_code, "ms": ms, "tenant": getattr(user, "tenant_slug", None), "instance": s.instance_id}},
            )
        return response

    for r in (auth.router, chat.router, agent.router, documents.router, tickets.router, admin.router, internal.router):
        app.include_router(r)

    @app.api_route("/api/ping", methods=["GET", "HEAD"])
    async def ping():
        return {"ok": True, "instance": s.instance_id}

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    async def health():
        checks = {}
        try:
            await get_pool().fetchval("SELECT 1")
            checks["postgres"] = "ok"
        except Exception as e:
            checks["postgres"] = f"error: {e}"
        try:
            await get_redis().ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"
        checks["rabbitmq"] = "ok" if broker.ready else "down"
        ok = checks["postgres"] == "ok" and checks["redis"] == "ok"
        return JSONResponse({"status": "ok" if ok else "degraded", "instance": s.instance_id, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/api/config")
    async def public_config():
        return {"ai_backend": s.ai_backend, "embed_backend": s.embed_backend, "retriever": s.retriever, "agent_mode": s.agent_mode, "chunk_strategy": s.chunk_strategy, "instance": s.instance_id}

    return app


app = create_app()
