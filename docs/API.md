# API contract

The live OpenAPI schema at `/openapi.json` and Swagger UI at `/docs` are authoritative for request and response fields. All application routes use `/api/v1`. Timestamps require UTC offsets. IDs are UUIDs.

## Authentication and access

`Authorization: Bearer <access_token>` is required unless a route is marked public. Patients can see their own consultations and prescriptions; doctors can see their assigned consultations/prescriptions; admins can see all consultations and aggregate analytics. An admin creates doctor records for registered users. Admin bootstrap enables TOTP from the beginning. Role changes take effect on the next request because the database role is checked, not trusted from the JWT.

| Method and path | Access | Purpose |
| --- | --- | --- |
| POST `/auth/register` | Public | Patient registration; password at least 12 characters |
| POST `/auth/login` | Public | Access/refresh tokens; TOTP code if enabled |
| POST `/auth/refresh` | Public with refresh token | One-time refresh rotation |
| GET `/auth/me` | User | Own non-sensitive account fields |
| GET/PATCH `/auth/profile` | User | Read/update own name |
| POST `/auth/logout` | User | Invalidate all current tokens |
| POST `/auth/mfa/setup`, `/auth/mfa/enable?code=123456` | User | TOTP enrollment |
| DELETE `/auth/me` | User | Soft delete and disable account |
| POST `/doctors/admin/{user_id}` | Admin | Promote/verify doctor |
| GET `/doctors` | Public | Search specialty and availability |
| POST `/doctors/slots` | Doctor | Add future non-overlapping slot |
| DELETE `/doctors/slots/{slot_id}` | Owning doctor | Remove an unbooked slot |
| GET `/doctors/{doctor_id}/slots` | Public | Open future slots |
| POST `/consultations` | Patient | Book with `Idempotency-Key` |
| GET `/consultations`, `/{id}` | User | Filter own/all permitted consultations |
| PATCH `/consultations/{id}/status` | Participant/admin | Valid state transition |
| POST `/consultations/{id}/prescription` | Treating doctor | Write after completion |
| GET `/consultations/{id}/prescription` | Patient/doctor/admin | Audited sensitive read |
| GET `/admin/analytics` | Admin | Counts, revenue, daily trend |
| POST `/admin/consultations/{id}/payment` | Admin | Create internal pending payment |
| PATCH `/admin/payments/{id}` | Admin | Advance internal payment state |
| GET `/admin/audit-logs` | Admin | Paginated audit events |

`GET /health/live` and `GET /health/ready` are unversioned. `/metrics` is intended for a restricted Prometheus scrape path.

## Pagination and filters

Doctor search accepts `specialty`, `available_after`, `limit`, and `offset`. Slot search accepts `after`, `limit`, and `offset`. Consultations accept `status`, `created_after`, `created_before`, `limit`, and `offset`. Analytics accepts `days` (1–90). List responses return `items`, `total`, `limit`, and `offset`; the maximum page size is 100.

## Idempotency and errors

Booking and payment writes require `Idempotency-Key` of 8–128 characters. Scope is actor plus operation. Repeating an identical booking request returns the original consultation with `Idempotency-Replayed: true`; the same key with different input returns 409. For booking, a different key aimed at a booked slot returns 409. Payment status transitions are similarly keyed and bounded by allowed states.

Errors return `{"error":"message","request_id":"..." }`; 422 responses include a `details` array. Common statuses: 400 invalid MFA, 401 missing/bad credentials, 403 forbidden role or ownership, 404 missing resource, 409 conflict or invalid transition, 422 invalid input, 429 rate limit, 503 missing critical dependency, 500 unexpected error. Internal exception details are never returned. Supply `X-Request-ID` for correlation or let the server generate one.
