import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AuditLog, Doctor, Role, User


def register(client, email):
    created = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "verification-password-12345",
            "full_name": "Original Name",
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "verification-password-12345"}
    )
    assert login.status_code == 200, login.text
    return created.json()["id"], {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_profile_filters_slot_management_and_inactive_doctor(client):
    doctor_id, doctor_headers = register(client, "final-doctor@example.com")
    _, patient_headers = register(client, "final-patient@example.com")

    assert client.get("/api/v1/auth/profile").status_code == 401
    assert (
        client.get("/api/v1/auth/profile", headers=patient_headers).json()["full_name"]
        == "Original Name"
    )
    updated = client.patch(
        "/api/v1/auth/profile", json={"full_name": "New Name"}, headers=patient_headers
    )
    assert updated.status_code == 200 and updated.json()["full_name"] == "New Name"
    assert (
        client.patch(
            "/api/v1/auth/profile", json={"full_name": ""}, headers=patient_headers
        ).status_code
        == 422
    )

    with SessionLocal() as db:
        user = db.get(User, uuid.UUID(doctor_id))
        user.role = Role.doctor
        db.add(
            Doctor(
                user_id=user.id, specialty="Neurology", license_number="LIC-FINAL", is_verified=True
            )
        )
        db.commit()

    start = datetime.now(UTC) + timedelta(days=3)
    slot_ids = []
    for offset in range(3):
        beginning = start + timedelta(days=offset)
        response = client.post(
            "/api/v1/doctors/slots",
            json={
                "starts_at": beginning.isoformat(),
                "ends_at": (beginning + timedelta(minutes=30)).isoformat(),
            },
            headers=doctor_headers,
        )
        assert response.status_code == 201, response.text
        slot_ids.append(response.json()["id"])

    doctors = client.get(
        "/api/v1/doctors",
        params={
            "specialty": "neuro",
            "available_after": (start - timedelta(hours=1)).isoformat(),
            "limit": 1,
            "offset": 0,
        },
    )
    assert doctors.status_code == 200 and doctors.json()["total"] == 1
    assert len(doctors.json()["items"]) == 1
    assert client.get("/api/v1/doctors", params={"limit": 101}).status_code == 422
    assert (
        client.get(f"/api/v1/doctors/{doctor_id}/slots", params={"limit": 1}).json()["total"] == 3
    )

    assert (
        client.delete(f"/api/v1/doctors/slots/{slot_ids[0]}", headers=patient_headers).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/doctors/slots/{slot_ids[0]}", headers=doctor_headers).status_code
        == 204
    )
    assert client.get(f"/api/v1/doctors/{doctor_id}/slots").json()["total"] == 2

    assert (
        client.post(
            "/api/v1/consultations",
            json={"slot_id": str(uuid.uuid4())},
            headers={**patient_headers, "Idempotency-Key": "missing-slot-001"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/consultations",
            json={"slot_id": slot_ids[1]},
            headers={"Idempotency-Key": "missing-auth-001"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/consultations",
            json={"slot_id": slot_ids[1]},
            headers={**doctor_headers, "Idempotency-Key": "doctor-booking-001"},
        ).status_code
        == 403
    )
    booking = client.post(
        "/api/v1/consultations",
        json={"slot_id": slot_ids[1]},
        headers={**patient_headers, "Idempotency-Key": "final-booking-001"},
    )
    assert booking.status_code == 201, booking.text
    listing = client.get(
        "/api/v1/consultations",
        params={
            "status": "scheduled",
            "created_after": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "limit": 1,
            "offset": 0,
        },
        headers=patient_headers,
    )
    assert listing.status_code == 200 and listing.json()["total"] == 1
    assert (
        client.get(
            "/api/v1/consultations",
            params={"created_after": "2026-01-01T00:00:00"},
            headers=patient_headers,
        ).status_code
        == 422
    )

    with SessionLocal() as db:
        user = db.get(User, uuid.UUID(doctor_id))
        user.is_active = False
        db.commit()
    assert client.get("/api/v1/doctors", params={"specialty": "neuro"}).json()["total"] == 0
    assert client.get(f"/api/v1/doctors/{doctor_id}/slots").status_code == 404
    assert (
        client.post(
            "/api/v1/consultations",
            json={"slot_id": slot_ids[2]},
            headers={**patient_headers, "Idempotency-Key": "inactive-doctor-001"},
        ).status_code
        == 409
    )
    with SessionLocal() as db:
        assert (
            db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "profile.updated"))
            == 1
        )
        assert (
            db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "booking.created"))
            == 1
        )
        assert (
            db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "slot.deleted")) == 1
        )


def test_parallel_identical_booking_key_replays(client):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.main import app

    doctor_id, doctor_headers = register(client, "samekey-doctor@example.com")
    _, patient_headers = register(client, "samekey-patient@example.com")
    with SessionLocal() as db:
        user = db.get(User, uuid.UUID(doctor_id))
        user.role = Role.doctor
        db.add(
            Doctor(
                user_id=user.id, specialty="General", license_number="LIC-SAMEKEY", is_verified=True
            )
        )
        db.commit()
    start = datetime.now(UTC) + timedelta(days=4)
    slot = client.post(
        "/api/v1/doctors/slots",
        json={
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(minutes=30)).isoformat(),
        },
        headers=doctor_headers,
    )
    assert slot.status_code == 201
    slot_id = slot.json()["id"]

    def attempt(_):
        with TestClient(app) as local:
            return local.post(
                "/api/v1/consultations",
                json={"slot_id": slot_id},
                headers={**patient_headers, "Idempotency-Key": "parallel-same-key-001"},
            )

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(attempt, range(5)))
    assert [response.status_code for response in responses] == [201] * 5, [
        response.text for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    with SessionLocal() as db:
        from app.models import Consultation, IdempotencyRecord

        assert db.scalar(select(func.count(Consultation.id))) == 1
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 1


def test_booking_failure_metric_counts_auth_rejections(client):
    import re

    def failures():
        metrics = client.get("/metrics").text
        match = re.search(r"^amrutam_booking_failures_total ([0-9.]+)$", metrics, re.M)
        assert match is not None
        return float(match.group(1))

    before = failures()
    response = client.post(
        "/api/v1/consultations",
        json={"slot_id": str(uuid.uuid4())},
        headers={"Idempotency-Key": "metric-failure-001"},
    )
    assert response.status_code == 401
    assert failures() == before + 1


def test_redis_registration_rate_limit_and_ttl(client):
    import os

    import redis

    body = {
        "email": "rate-limit@example.com",
        "password": "rate-limit-password-2026",
        "full_name": "Rate Limit",
    }
    statuses = [client.post("/api/v1/auth/register", json=body).status_code for _ in range(11)]
    assert statuses == [201] + [409] * 9 + [429]
    cache = redis.Redis.from_url(os.environ["REDIS_URL"])
    keys = list(cache.scan_iter(match="rate:register:*"))
    assert len(keys) == 1
    assert 0 < cache.ttl(keys[0]) <= 3600
