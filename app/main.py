import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text

from app import admin, auth, consultations, doctors
from app.config import get_settings
from app.db import engine

request_id_var = ContextVar("request_id", default="-")
REQUESTS = Counter("amrutam_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("amrutam_http_request_seconds", "HTTP request latency", ["method", "route"])
BOOKING_FAILURES = Counter("amrutam_booking_failures_total", "Failed booking attempts")
logger = logging.getLogger("amrutam")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "service": "api",
            "message": record.getMessage(),
        }
        for field in ("method", "route", "status", "latency_ms", "error_type"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)
settings = get_settings()
app = FastAPI(
    title="Amrutam Telemedicine API",
    version="1.0.0",
    description="Versioned REST API for telemedicine workflows",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in settings.allowed_origins.split(",") if x.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(doctors.router, prefix="/api/v1")
app.include_router(consultations.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")

if settings.otlp_endpoint:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", request_id):
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error("Unhandled request error", extra={"error_type": type(exc).__name__})
        response = JSONResponse(
            status_code=500, content={"error": "Internal server error", "request_id": request_id}
        )
    finally:
        request_id_var.reset(token)
    route = request.scope.get("route")
    path = route.path if route else "unmatched"
    REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    elapsed = time.perf_counter() - start
    LATENCY.labels(request.method, path).observe(elapsed)
    logger.info(
        "request.completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "route": path,
            "status": response.status_code,
            "latency_ms": round(elapsed * 1000, 2),
        },
    )
    if (
        request.url.path == "/api/v1/consultations"
        and request.method == "POST"
        and response.status_code >= 400
    ):
        BOOKING_FAILURES.inc()
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    if getattr(request.state, "replayed", False):
        response.headers["Idempotency-Replayed"] = "true"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "request_id": request.state.request_id},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation failed",
            "request_id": request.state.request_id,
            "details": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        },
    )


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        redis.Redis.from_url(settings.redis_url, socket_timeout=1).ping()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
