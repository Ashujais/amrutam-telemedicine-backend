# Five-minute demo runbook

This is a **recording runbook**, not a recorded video. It calls the real Docker API and checks every response. Allow about five minutes to narrate the seven beats below; `scripts/demo.py` itself usually completes faster. Run it against a local throwaway environment because it creates demo users, slots, consultations and a prescription.

## One-time setup (before recording)

Prerequisites: Python 3.12 with `python -m pip install -e ".[dev]"`, Docker Desktop/Engine with Compose, and a repository-root `.env` as described in [README](README.md). Set a random `POSTGRES_PASSWORD`, matching `DATABASE_URL` password, `JWT_SECRET`, and `MFA_ENCRYPTION_KEY` in `.env`; keep that file private. Then run from the repository root:

```powershell
docker compose up --build -d
docker compose ps
curl.exe -i http://127.0.0.1:8000/health/ready
```

Bootstrap an administrator **once** on this local database. Use a unique email and a password of at least 12 characters. Save/scan the printed TOTP enrollment URI; it is shown once. If an admin already exists, use its credentials instead.

```powershell
$env:ADMIN_EMAIL = 'admin@example.test'
$env:ADMIN_PASSWORD = Read-Host 'New local admin password'
docker compose exec -T -e ADMIN_EMAIL -e ADMIN_PASSWORD api python -m app.bootstrap_admin
Remove-Item Env:ADMIN_PASSWORD
```

Run the complete API demonstration. The command prompts for the admin password and current six-digit TOTP code, and stops immediately if any expected status differs. It generates unique demo patient/doctor accounts automatically and never prints tokens or passwords.

```powershell
python scripts/demo.py --admin-email admin@example.test
```

For a separate, small latency sample after recording, rerun with `--benchmark`. It creates additional local data and measures ten sequential requests for each operation:

```powershell
python scripts/demo.py --admin-email admin@example.test --benchmark
```

## Recording beats and exact API calls

The script prints each method/path/status as it executes. Bodies below are the exact JSON shape; UUIDs, future ISO timestamps, generated emails and bearer tokens are supplied by the script. All paths are under `http://127.0.0.1:8000`.

| Time | Show | Exact request(s) and expected result |
| --- | --- | --- |
| 0:00–0:30 | Health, docs, metrics | `GET /health/live`, `/health/ready`, `/docs`, `/openapi.json`, `/metrics` → 200. Show `docker compose ps` healthy. |
| 0:30–1:10 | Authentication and RBAC | `POST /api/v1/auth/register` with `{"email":"...","password":"...","full_name":"..."}` → 201 for doctor candidate and two patients; `POST /api/v1/auth/login` with `{"email":"...","password":"..."}` → 200. Admin login adds `"mfa_code":"123456"`. Patient `GET /api/v1/admin/analytics` → 403. |
| 1:10–1:45 | Doctor availability and search | Admin `POST /api/v1/doctors/admin/{doctor_user_id}` with `{"specialty":"Cardiology","license_number":"DEMO-..."}` → 201. Doctor `POST /api/v1/doctors/slots` with `{"starts_at":"future ISO UTC","ends_at":"30 minutes later"}` → 201. `GET /api/v1/doctors?specialty=card&limit=1`, `GET /api/v1/doctors?available_after={encoded ISO time}&limit=1`, `GET /api/v1/doctors/{id}/slots?limit=1` → 200. |
| 1:45–2:40 | Booking, replay and race | Patient `POST /api/v1/consultations` with `{"slot_id":"UUID"}` and `Idempotency-Key: demo-booking-...` → 201; exact replay → 201 with same consultation ID and `Idempotency-Replayed: true`. Other patient/key for occupied slot → 409. The script creates a second slot and sends two simultaneous patient bookings: exactly one 201 and one 409. |
| 2:40–3:30 | Lifecycle and prescription | Treating doctor `PATCH /api/v1/consultations/{id}/status` with `{"status":"completed"}` directly from scheduled → 409; then `confirmed`, `in_progress`, `completed` → 200 each. Doctor `POST /api/v1/consultations/{id}/prescription` with `{"medication":"Demo medicine","instructions":"Demo instructions"}` → 201. Patient read → 200; other patient read → 403. |
| 3:30–4:15 | Filtering, admin analytics, audit | Patient `GET /api/v1/consultations?status=completed&limit=1` → 200. Admin `GET /api/v1/admin/analytics?days=7` and `/api/v1/admin/audit-logs?limit=5` → 200. |
| 4:15–5:00 | Operations proof | Revisit `/metrics` after requests and `/docs`; point to [architecture](docs/ARCHITECTURE.md), [security](SECURITY.md), [operations](docs/OPERATIONS.md), and `.github/workflows/ci.yml`. Explain that payment records are internal bookkeeping and production SLO/PITR/CD are not demonstrated. |

A standalone automated PostgreSQL concurrency/idempotency proof is available through `pytest -q tests/test_api.py tests/test_verification.py` with the disposable `_test` DB and Redis DB 1 described in README. The demo script itself also runs a two-patient HTTP race. **The video must still be recorded separately.**
