# Amrutam Telemedicine Backend

A FastAPI/PostgreSQL service for patient registration, doctor discovery and availability, concurrency-safe booking, consultations, prescriptions, auditable administrative operations, and analytics. The implementation is sized for a take-home assignment; the scale and latency targets are design goals, not benchmarked guarantees.

## Stack and layout

- Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL 16.
- Redis for login/registration rate limiting and one-time refresh-token rotation.
- Argon2id password hashing, HS256 JWTs, encrypted TOTP seeds, role checks, Prometheus metrics, JSON request logs, optional OpenTelemetry OTLP traces.
- `app/`: API routes, models, schemas, security, services, and bootstrap command.
- `alembic/`: versioned schema migration.
- `tests/`: unit and PostgreSQL/Redis API tests, including simultaneous booking.
- `docs/`: architecture and API contract details.

## Configuration

Copy `.env.example` to `.env`. Change `POSTGRES_PASSWORD` and make the password in `DATABASE_URL` match. Generate independent secrets:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Use the first output for `JWT_SECRET` and the second for `MFA_ENCRYPTION_KEY`. Keep `.env` private. `ALLOWED_ORIGINS` is a comma-separated list of trusted browser origins. `OTLP_ENDPOINT` is optional. Docker Compose uses the `db` and `redis` hostnames in the example file.

## Run with Docker

Prerequisites: Docker Engine / Docker Desktop with Compose. From the repository root:

```powershell
Copy-Item .env.example .env
# Edit .env with the generated secrets and matching database password.
docker compose up --build -d
docker compose ps
curl.exe http://localhost:8000/health/ready
```

The one-shot `migrate` service runs `alembic upgrade head` before the API starts. Open `http://localhost:8000/docs` for interactive OpenAPI documentation or `/openapi.json` for the schema.

Bootstrap the first administrator after migration. Give it a unique email and password; scan the one-time TOTP URI printed by the command.

```powershell
$env:ADMIN_PASSWORD = "choose-a-long-unique-password"
docker compose exec -e ADMIN_EMAIL=admin@example.com -e ADMIN_PASSWORD api python -m app.bootstrap_admin
Remove-Item Env:ADMIN_PASSWORD
```

The administrator account starts with MFA enabled. To develop without Docker for the API, install PostgreSQL and Redis, change the `.env` connection URLs to `localhost`, then run:

```powershell
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

## Main workflows

1. Patients register at `POST /api/v1/auth/register`, log in at `/auth/login`, optionally enroll TOTP with `/auth/mfa/setup` and `/auth/mfa/enable`, and rotate refresh tokens once at `/auth/refresh`.
2. An admin promotes a registered user at `POST /api/v1/doctors/admin/{user_id}`; the doctor publishes non-overlapping future slots at `POST /api/v1/doctors/slots`.
3. Patients search `GET /api/v1/doctors` and available slots, then book with `POST /api/v1/consultations` and an `Idempotency-Key` header (8–128 characters). Repeating the same key and request returns the original consultation with `Idempotency-Replayed: true`; reusing a key with different data returns 409. A competing patient receives 409.
4. A doctor moves a consultation through `scheduled → confirmed → in_progress → completed`. Patient cancellation is allowed from scheduled or confirmed. The doctor can create one prescription after completion. Patient and treating doctor can retrieve it.
5. Admins can record an internal payment state and view `/api/v1/admin/analytics`. Payment endpoints are an administrative stand-in; there is no real payment capture or webhook verification. They require idempotency keys.

All list endpoints use `limit` (1–100) and `offset` (0+). Errors have `error` and `request_id`; validation errors also include `details`. Access tokens expire in 15 minutes by default. Refresh tokens expire in seven days and each can be used once. Logout/deactivation invalidates all tokens through a user token version.

### Example calls

```powershell
curl.exe -X POST http://localhost:8000/api/v1/auth/register -H "Content-Type: application/json" -d '{"email":"patient@example.com","password":"a-long-unique-password","full_name":"Example Patient"}'
curl.exe -X POST http://localhost:8000/api/v1/auth/login -H "Content-Type: application/json" -d '{"email":"patient@example.com","password":"a-long-unique-password"}'
# Copy access_token from login:
curl.exe -X POST http://localhost:8000/api/v1/consultations -H "Authorization: Bearer ACCESS_TOKEN" -H "Idempotency-Key: booking-demo-001" -H "Content-Type: application/json" -d '{"slot_id":"SLOT_UUID"}'
```

PowerShell's `curl.exe` is used explicitly because `curl` may be an alias on older Windows shells. The interactive docs are easiest for the full demo.

## Checks

```powershell
ruff check .
python -m compileall -q app alembic tests
pytest -q
docker build -t amrutam-api:local .
```

The integration tests need a reachable PostgreSQL test database and Redis. Compose binds both to localhost for development; create a separate `amrutam_test` database before pointing tests at it. The test fixture runs the migration and truncates test tables. Without those services, integration tests are skipped; CI supplies both and runs them. Set `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, and `MFA_ENCRYPTION_KEY` before local tests. With Compose running, this PowerShell sequence creates a disposable test database and runs the complete suite:

```powershell
docker compose exec -T db createdb -U amrutam amrutam_test
$line = Get-Content .env | Where-Object { $_ -like "DATABASE_URL=*" } | Select-Object -First 1
$base = $line.Substring("DATABASE_URL=".Length).Replace("@db:", "@127.0.0.1:")
$env:DATABASE_URL = $base.Substring(0, $base.LastIndexOf("/")) + "/amrutam_test?connect_timeout=3"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
$env:REQUIRE_INTEGRATION = "1"
pytest -q
```

The `createdb` command is needed only once. Tests use a test-only JWT/MFA key unless those variables are already set; never point them at a production database. CI also runs `pip-audit` and builds the image. The Docker build and live integration checks require a running Docker engine.

## Operations and trade-offs

`/health/live` checks process life; `/health/ready` checks PostgreSQL and Redis. `/metrics` exposes request and booking failure metrics; protect it at the ingress in production. Set `OTLP_ENDPOINT` to export FastAPI and SQLAlchemy spans. Logs are JSON with request IDs and no medical payloads.

Booking holds a row lock on the slot, marks it booked, creates the consultation, idempotency record, and audit entry in one transaction. A unique consultation-slot key and an exclusion constraint on doctor time ranges are database backstops. Cancelled slots remain retired to retain a simple one-slot/one-consultation history; create a new slot to reschedule. Redis is not used as a booking lock or medical cache. API replicas are stateless; Redis is required for auth-rate limiting and refresh replay defense.

See [architecture](docs/ARCHITECTURE.md), [API details](docs/API.md), [security and threat model](SECURITY.md), and [five-minute demo](DEMO.md). Production deployment needs a managed PostgreSQL/Redis setup, TLS ingress, secret manager, backup/PITR, restricted metrics, and measured load tests before claiming the target p95 or availability objectives.
