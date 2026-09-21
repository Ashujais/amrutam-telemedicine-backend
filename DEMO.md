# Five-minute demo

Prerequisites: Docker Desktop/Engine running, `.env` created with real random secrets and matching PostgreSQL password, and `docker compose up --build -d` healthy. Use `http://localhost:8000/docs` for requests. The first admin is bootstrapped with `python -m app.bootstrap_admin` inside the API container as shown in README; scan its printed TOTP URI.

1. **0:00–0:30 — Start and inspect.** Run `docker compose ps`, open `/health/ready`, `/docs`, and `/metrics`. Show the PostgreSQL, Redis, migration, and API services.
2. **0:30–1:10 — Accounts and RBAC.** Register a patient and a doctor candidate with `POST /api/v1/auth/register`. Log each in and copy access tokens. Show a patient receives 403 from `GET /api/v1/admin/analytics`. Log in as the bootstrap admin with its current six-digit TOTP code.
3. **1:10–1:45 — Doctor and availability.** As admin call `POST /api/v1/doctors/admin/{doctor_user_id}` with `{"specialty":"Cardiology","license_number":"DEMO-001"}`. As doctor publish a 30-minute slot starting tomorrow via `POST /api/v1/doctors/slots`. Search `GET /api/v1/doctors?specialty=card` and list the doctor's slots.
4. **1:45–2:40 — Booking safety.** As patient call `POST /api/v1/consultations` with `{"slot_id":"..."}` and `Idempotency-Key: demo-booking-001`. Repeat exactly: same consultation ID and `Idempotency-Replayed: true`. Try another patient/key for the same slot: 409. Mention `pytest tests/test_api.py -q` runs a simultaneous two-patient race against PostgreSQL in CI.
5. **2:40–3:30 — Clinical flow.** As doctor PATCH the consultation through `confirmed`, `in_progress`, and `completed`. A direct `scheduled → completed` transition returns 409. Create a prescription, read it as the patient, and show another patient receives 403.
6. **3:30–4:15 — Admin and audit.** As admin create a pending internal payment with an idempotency key, view analytics and paginated audit logs. Explain that payment status is internal bookkeeping and no money is collected.
7. **4:15–5:00 — Review and operations.** Show OpenAPI role contracts, architecture diagrams, security checklist, CI workflow, readiness, and request metrics. Point to the backup/DR and partitioning sections in `docs/ARCHITECTURE.md`.

For an automated proof of the booking race, run `pytest -q` with `DATABASE_URL` pointing to a disposable PostgreSQL database and `REDIS_URL` to Redis. The test suite skips DB tests when those services are unavailable locally; CI provisions both.
