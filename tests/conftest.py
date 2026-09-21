import os

import pytest
import redis
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from alembic import command

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://amrutam:test-password@localhost:5432/amrutam_test?connect_timeout=2",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-thirty-two-characters")
os.environ.setdefault("MFA_ENCRYPTION_KEY", "D5KMy6hAr2zk5Ig76orQPtLXAHXv1PbWrqvXA3aZqDE=")

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def dependencies():
    if not (engine.url.database or "").endswith("_test"):
        pytest.fail("Refusing to migrate or truncate a database without a _test suffix")
    test_redis = redis.Redis.from_url(os.environ["REDIS_URL"])
    if test_redis.connection_pool.connection_kwargs.get("db") != 1:
        pytest.fail("Refusing to flush Redis outside dedicated test DB 1")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        test_redis.ping()
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION") == "1":
            pytest.fail(f"Required integration dependencies unavailable: {exc}")
        pytest.skip("PostgreSQL and Redis are required for integration tests")
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def client(dependencies):
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE audit_logs, idempotency_records, prescriptions, payments, "
                "consultations, availability_slots, doctors, profiles, users "
                "RESTART IDENTITY CASCADE"
            )
        )
    redis.Redis.from_url(os.environ["REDIS_URL"]).flushdb()
    with TestClient(app) as test_client:
        yield test_client
