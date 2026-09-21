import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Doctor, Role, User
from app.security import encrypt_mfa, hash_password


def register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "full_name": email.split("@")[0],
        },
    )
    assert response.status_code == 201, response.text
    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
        },
    )
    assert login.status_code == 200, login.text
    return response.json()["id"], {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_auth_mfa_and_errors(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    user_id, headers = register(client, "one@example.com")
    assert (
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "one@example.com",
                "password": "correct-horse-battery-staple",
                "full_name": "One",
            },
        ).status_code
        == 409
    )
    assert client.get("/api/v1/auth/me", headers=headers).json()["id"] == user_id
    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "one@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    refresh_token = login.json()["refresh_token"]
    assert (
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code
        == 200
    )
    assert (
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code
        == 401
    )
    setup = client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert (
        client.post(
            "/api/v1/auth/mfa/enable", params={"code": pyotp.TOTP(secret).now()}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={
                "email": "one@example.com",
                "password": "correct-horse-battery-staple",
            },
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        from sqlalchemy import func, select

        from app.models import AuditLog

        mfa_setups = db.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.action == "auth.mfa_setup")
        )
        assert mfa_setups == 1


def test_booking_race_lifecycle_and_access(client):
    admin_id = uuid.uuid4()
    admin_secret = pyotp.random_base32()
    with SessionLocal() as db:
        db.add(
            User(
                id=admin_id,
                email="admin@example.com",
                password_hash=hash_password("admin-password-very-long"),
                role=Role.admin,
                mfa_secret_encrypted=encrypt_mfa(admin_secret),
                mfa_enabled=True,
            )
        )
        db.commit()
    admin_login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@example.com",
            "password": "admin-password-very-long",
            "mfa_code": pyotp.TOTP(admin_secret).now(),
        },
    )
    assert admin_login.status_code == 200, admin_login.text
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-very-long"},
        ).status_code
        == 401
    )
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    doctor_id, doctor_headers = register(client, "doctor@example.com")
    patient_id, patient_headers = register(client, "patient@example.com")
    _, stranger_headers = register(client, "stranger@example.com")
    assert client.get("/api/v1/admin/analytics", headers=patient_headers).status_code == 403
    promoted = client.post(
        f"/api/v1/doctors/admin/{doctor_id}",
        json={
            "specialty": "Cardiology",
            "license_number": "LIC-001",
        },
        headers=admin_headers,
    )
    assert promoted.status_code == 201, promoted.text
    # Existing access token remains tied to the same user; role is read from DB.
    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(minutes=30)
    slot = client.post(
        "/api/v1/doctors/slots",
        json={
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
        },
        headers=doctor_headers,
    )
    assert slot.status_code == 201, slot.text
    slot_id = slot.json()["id"]
    overlap = client.post(
        "/api/v1/doctors/slots",
        json={
            "starts_at": (start + timedelta(minutes=5)).isoformat(),
            "ends_at": (end + timedelta(minutes=5)).isoformat(),
        },
        headers=doctor_headers,
    )
    assert overlap.status_code == 409, overlap.text
    assert (
        client.get("/api/v1/doctors", params={"specialty": "card", "limit": 10}).json()["total"]
        == 1
    )

    def attempt(key, headers):
        with TestClient(app) as local_client:
            return local_client.post(
                "/api/v1/consultations",
                json={"slot_id": slot_id},
                headers={**headers, "Idempotency-Key": key},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda data: attempt(*data),
                [
                    ("patient-booking-001", patient_headers),
                    ("stranger-booking-001", stranger_headers),
                ],
            )
        )
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    winner = results[0] if results[0].status_code == 201 else results[1]
    winner_headers = patient_headers if results[0].status_code == 201 else stranger_headers
    loser_headers = stranger_headers if results[0].status_code == 201 else patient_headers
    winning_key = "patient-booking-001" if results[0].status_code == 201 else "stranger-booking-001"
    consultation_id = winner.json()["id"]
    replay = attempt(winning_key, winner_headers)
    assert replay.status_code == 201 and replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == consultation_id
    another_start = start + timedelta(days=1)
    another_slot = client.post(
        "/api/v1/doctors/slots",
        json={
            "starts_at": another_start.isoformat(),
            "ends_at": (another_start + timedelta(minutes=30)).isoformat(),
        },
        headers=doctor_headers,
    )
    assert another_slot.status_code == 201
    different_payload = client.post(
        "/api/v1/consultations",
        json={"slot_id": another_slot.json()["id"]},
        headers={**winner_headers, "Idempotency-Key": winning_key},
    )
    assert different_payload.status_code == 409
    assert (
        client.get(f"/api/v1/consultations/{consultation_id}", headers=loser_headers).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/consultations/{consultation_id}/status",
            json={
                "status": "completed",
            },
            headers=doctor_headers,
        ).status_code
        == 409
    )
    for status in ("confirmed", "in_progress", "completed"):
        response = client.patch(
            f"/api/v1/consultations/{consultation_id}/status",
            json={
                "status": status,
            },
            headers=doctor_headers,
        )
        assert response.status_code == 200, response.text
    prescription = client.post(
        f"/api/v1/consultations/{consultation_id}/prescription",
        json={
            "medication": "Test medication",
            "instructions": "Once daily",
        },
        headers=doctor_headers,
    )
    assert prescription.status_code == 201, prescription.text
    assert (
        client.get(
            f"/api/v1/consultations/{consultation_id}/prescription", headers=winner_headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/consultations/{consultation_id}/prescription", headers=loser_headers
        ).status_code
        == 403
    )
    payment = client.post(
        f"/api/v1/admin/consultations/{consultation_id}/payment",
        json={
            "amount_minor": 10000,
            "currency": "INR",
        },
        headers={**admin_headers, "Idempotency-Key": "payment-create-001"},
    )
    assert payment.status_code == 201, payment.text
    replay_payment = client.post(
        f"/api/v1/admin/consultations/{consultation_id}/payment",
        json={"amount_minor": 10000, "currency": "INR"},
        headers={**admin_headers, "Idempotency-Key": "payment-create-001"},
    )
    assert replay_payment.status_code == 201
    assert replay_payment.json()["id"] == payment.json()["id"]
    assert (
        client.post(
            f"/api/v1/admin/consultations/{consultation_id}/payment",
            json={"amount_minor": 9999, "currency": "INR"},
            headers={**admin_headers, "Idempotency-Key": "payment-create-001"},
        ).status_code
        == 409
    )
    changed = client.patch(
        f"/api/v1/admin/payments/{payment.json()['id']}",
        json={"status": "succeeded", "provider_reference": "DEMO-RECEIPT"},
        headers={**admin_headers, "Idempotency-Key": "payment-status-001"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == "succeeded"
    repeated_status = client.patch(
        f"/api/v1/admin/payments/{payment.json()['id']}",
        json={"status": "succeeded", "provider_reference": "DEMO-RECEIPT"},
        headers={**admin_headers, "Idempotency-Key": "payment-status-001"},
    )
    assert repeated_status.status_code == 200
    assert repeated_status.json()["id"] == payment.json()["id"]
    assert (
        client.patch(
            f"/api/v1/admin/payments/{payment.json()['id']}",
            json={"status": "refunded", "provider_reference": "DEMO-RECEIPT"},
            headers={**admin_headers, "Idempotency-Key": "payment-status-001"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/admin/payments/{payment.json()['id']}",
            json={"status": "failed"},
            headers={**admin_headers, "Idempotency-Key": "payment-status-002"},
        ).status_code
        == 409
    )
    assert client.get("/api/v1/admin/audit-logs", headers=admin_headers).status_code == 200
    assert (
        client.get("/api/v1/admin/analytics", headers=admin_headers).json()["totals"]["completed"]
        == 1
    )


def test_cancelled_booking_is_retired(client):
    doctor_id, doctor_headers = register(client, "cancel-doctor@example.com")
    _, patient_headers = register(client, "cancel-patient@example.com")
    _, other_headers = register(client, "cancel-other@example.com")
    with SessionLocal() as db:
        doctor_user = db.get(User, uuid.UUID(doctor_id))
        doctor_user.role = Role.doctor
        db.add(
            Doctor(
                user_id=doctor_user.id,
                specialty="Dermatology",
                license_number="LIC-CANCEL",
                is_verified=True,
            )
        )
        db.commit()
    start = datetime.now(UTC) + timedelta(days=2)
    slot = client.post(
        "/api/v1/doctors/slots",
        json={
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(minutes=30)).isoformat(),
        },
        headers=doctor_headers,
    )
    assert slot.status_code == 201, slot.text
    booked = client.post(
        "/api/v1/consultations",
        json={"slot_id": slot.json()["id"]},
        headers={**patient_headers, "Idempotency-Key": "cancel-booking-001"},
    )
    assert booked.status_code == 201, booked.text
    consultation_id = booked.json()["id"]
    cancelled = client.patch(
        f"/api/v1/consultations/{consultation_id}/status",
        json={"status": "cancelled"},
        headers=patient_headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (
        client.patch(
            f"/api/v1/consultations/{consultation_id}/status",
            json={"status": "confirmed"},
            headers=doctor_headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/consultations/{consultation_id}/prescription",
            json={"medication": "none", "instructions": "none"},
            headers=doctor_headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/consultations",
            json={"slot_id": slot.json()["id"]},
            headers={**other_headers, "Idempotency-Key": "cancel-other-001"},
        ).status_code
        == 409
    )
