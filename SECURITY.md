# Security checklist and threat model

This implementation handles health data. Deploy it only behind TLS with network isolation, managed database encryption, and an operational access policy. The code is an assignment implementation, not a legal compliance certification.

## Controls checklist

- [x] Patient registration assigns only the patient role; admin bootstrap is an explicit local command.
- [x] Argon2id hashes passwords; no plaintext password storage.
- [x] Short-lived signed access JWTs, one-time refresh rotation in Redis, token-version revocation on logout/deactivation.
- [x] TOTP MFA enrollment with encrypted seed; bootstrap admins start with MFA enabled.
- [x] Database-backed RBAC and ownership checks on consultations and prescriptions.
- [x] Row-locked booking, unique consultation slot, idempotency records, and slot-overlap exclusion constraint.
- [x] Pydantic input validation, bounded pagination, SQLAlchemy parameterized queries, restricted CORS, secure response headers.
- [x] Redis-backed auth rate limits; fail closed if the limiter is unavailable.
- [x] Audit entries for registration/login, account changes, doctor creation, booking, transitions, prescription creation/read, and payments.
- [x] Structured request logs without payloads or tokens; request IDs and optional tracing.
- [x] CI dependency audit; images should also be scanned by the deployment registry.
- [ ] Configure production TLS, private network paths, encryption at rest, backup encryption, WAF, and ingress controls.
- [ ] Run penetration testing, load testing, restore drills, and jurisdiction-specific health/privacy review before production.

## Threat model

| Area | Asset/actor/surface | Threat | Mitigation | Residual risk |
| --- | --- | --- | --- | --- |
| Accounts | Patient, doctor, admin; login and refresh | Credential stuffing or stolen refresh token | Argon2id, IP rate limit, short JWT life, TOTP, one-time refresh rotation | Distributed botnets can evade IP limits; add per-account risk signals and edge controls |
| Clinical data | Consultations and prescriptions; REST API and database | Broken object authorization or leak | Database role/owner checks, minimal responses, no payload logging, audit reads | Privileged database/operator access requires operational controls |
| Booking | Availability/consultations; concurrent API replicas | Double booking and replay | PostgreSQL row lock, unique constraint, actor-scoped idempotency key | Client must reuse its key on retry; SLO under contention needs measurement |
| Admin/payment | Admin routes and payment records | Forged payment outcome or role escalation | Bootstrap-only admin, MFA, RBAC, state machine, audit, idempotency | Internal admin payment update is not a verified processor; never treat as settled cash |
| Infrastructure | DB, Redis, metrics, logs, OTLP | Secret leakage or denial of service | Env/secret manager, private network, readiness, bounded pools, restricted metrics | Example Compose exposes API for local use; production ingress and network policy are required |
| Dependencies | Python packages and container | Known CVE or supply-chain compromise | CI `pip-audit`, version bounds, reproducible build steps | Versions are bounded rather than lockfile-pinned; add lockfile and image digest pinning before production |

Attack surfaces are the public HTTP API and docs, health/metrics paths, database and Redis connections, container images, CI pipeline, logs, and administrative command. Actors include patients, doctors, admins, anonymous users, malicious clients, and infrastructure operators.

## Data classification, retention, and encryption

Prescription medication/instructions and consultation associations are restricted clinical data. Login credentials, TOTP seeds, JWT signing keys, and provider references are security-sensitive. Email/name are personal data. Metrics and logs should contain only operational metadata. Audit details must avoid medical text and tokens. Use least privilege for database accounts, separate audit access, and immutable exported audit storage in production.

The application encrypts TOTP seeds with Fernet; secrets are supplied through environment configuration. Deploy PostgreSQL and backups with volume/object encryption and enforce TLS for external clients, database connections, Redis, and OTLP. Rotate JWT keys by introducing a key ID and accepting old/new keys during a short overlap; rotate Fernet seeds with a multi-key decrypt/re-encrypt migration before retiring the old key. The current single-key implementation requires a coordinated maintenance rotation and invalidation of active tokens. Store keys in a managed secret service, never in the repository or image.

Choose retention with legal counsel and the applicable jurisdiction. As an operating proposal, retain clinical records according to statutory policy, audit events at least as long as required for investigation, and short-lived rate/refresh records only until their TTL. Deletion currently disables an account and preserves clinical referential integrity; identity erasure/anonymization requires a reviewed workflow. Backup retention and restored copies must follow the same deletion/retention policy.

## Incident response

On suspected token compromise, rotate the signing secret and force reauthentication. On Redis state loss, invalidate active refresh sessions. On clinical data exposure, preserve audit/log evidence, restrict access, rotate affected credentials, and follow applicable notification obligations. Test restoration from PostgreSQL PITR backups quarterly.
