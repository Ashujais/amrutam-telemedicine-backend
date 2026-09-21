# Operations, CI/CD, tracing, and recovery

## CI is implemented; CD is a deployment plan

`.github/workflows/ci.yml` runs on pushes and pull requests. It checks out the repo, installs Python 3.12 and dependencies, lints, compiles, migrates a PostgreSQL 16 service, runs tests with Redis 7, audits Python dependencies and builds the Docker image. The test database is `amrutam_test`; tests use Redis DB 1. The workflow YAML parses locally and its commands have been run locally, but **a hosted GitHub Actions run has not been observed**.

Local reproduction from a running Compose stack is described in README. The relevant commands are:

```powershell
ruff check .
python -m compileall -q app alembic tests scripts
alembic upgrade head  # DATABASE_URL must point to disposable amrutam_test
pytest -q             # REQUIRE_INTEGRATION=1; Redis DB 1
pip-audit --skip-editable
docker build -t amrutam-api:local .
```

Set the test environment values shown in `.github/workflows/ci.yml` and README before migration/tests. Do not run the migration or integration fixture against the main database.

There is **no CD job or deployment target** in this repository. A production release workflow would: publish a versioned, scanned image to a registry; inject secrets from a secret manager; run Alembic once as a controlled release job; deploy at least two stateless API replicas behind TLS ingress and readiness probes; verify health/metrics; then promote traffic and retain a rollback image. The actual cloud provider, registry, identity, ingress, metrics ACL, database/Redis services, backup service and rollback automation must be selected and implemented before calling this CD. Local Compose is a developer stack only.

## Logs, metrics and optional tracing

The API emits JSON request logs with timestamp, level, request ID, method, route, status and latency; it does not log payloads, JWTs or clinical text. `/metrics` exposes request counters by status/route, latency histograms and booking failures. Restrict metrics through production ingress; the local Compose port is loopback-bound.

OTLP tracing is **off by default**. Set `OTLP_ENDPOINT` to the collector's full HTTP traces path, for example `http://collector:4318/v1/traces`. `app/main.py` instruments FastAPI and SQLAlchemy and exports OTLP protobuf over HTTP. A temporary local receiver can verify that spans leave a temporary API container:

```powershell
python scripts/trace_smoke.py
```

This smoke test starts a local HTTP receiver, launches a separate API container with `OTLP_ENDPOINT` pointing to the host receiver, makes readiness calls, decodes the protobuf trace export, then removes the container. It does not install a production collector, retention store, sampling policy or alerting. The local verification on 2026-09-21 received **22 spans in one export request**.

## PostgreSQL backup, restore and disaster recovery

**Local dump/restore drill:** With Compose PostgreSQL running and a disposable `amrutam_test` database, run:

```powershell
python scripts/backup_restore_drill.py --source amrutam_test
```

The script refuses a source name without `_test`, uses `pg_dump -Fc`, restores to a uniquely named `_test` database, compares Alembic revision and nine table counts, then removes the temporary dump and restored DB. Local result on 2026-09-21: **24,414-byte dump, revision `0001`, nine table counts matched**. This verifies logical dump/restore on local data; it does **not** test production backups or point-in-time recovery.

For an operator-managed logical backup, the corresponding PostgreSQL commands are `pg_dump -Fc -d SOURCE > backup.dump`, `createdb RESTORE_TARGET`, and `pg_restore --no-owner --no-acl -d RESTORE_TARGET backup.dump`; run them with authorized credentials in an isolated environment and validate schema version, row counts and critical consultation/booking relationships before switching traffic. Encrypt and access-control the backup file and delete transient local copies.

**Production design, not operational in this repo:** Use managed PostgreSQL with daily full base backups, continuous WAL archiving, encrypted second-region copies and a 30-day operational retention proposal (subject to clinical/legal policy). Target **RPO 15 minutes** and **RTO 4 hours** only after restore drills demonstrate them. Monitor backup completion and WAL archive lag. For a PITR event, stop writes, select a clean recovery timestamp, restore a base backup to a **new** cluster, replay archived WAL to that timestamp using the provider's recovery controls, validate Alembic/schema and critical booking counts, then redirect traffic. Never overwrite the only primary during a drill. Test the full procedure at least quarterly, including zone/region loss and credential restoration. No WAL archive, PITR restore or RPO/RTO measurement was performed locally.

Redis contains short-lived rate counters and used refresh IDs, not clinical source-of-truth data. After total Redis loss, invalidate outstanding refresh sessions/force reauthentication before returning traffic.

## Small local latency sample

`python scripts/demo.py --admin-email admin@example.test --benchmark` runs the full HTTP demo and then measures ten **sequential** requests each for doctor search, profile update and booking against warm local Docker. Setup and slot creation are excluded from each timed operation. Values measured on 2026-09-21 (milliseconds, nearest-rank p95 of only ten samples):

| Operation | n | Median | p95 | Max |
| --- | ---: | ---: | ---: | ---: |
| Doctor search read | 10 | 16.2 | 52.6 | 52.6 |
| Profile update write | 10 | 21.5 | 89.2 | 89.2 |
| Booking write | 10 | 34.6 | 101.1 | 101.1 |

With ten samples, nearest-rank p95 is the maximum observed value. These are **local benchmark samples**, not a load test, production p95, 100k/day capacity proof, or 99.95% availability measurement. Results depend on the host, database state and concurrency; a production-like load/soak test remains necessary.
