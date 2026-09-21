"""Run the five-minute API demo and, optionally, a small local latency sample."""

import argparse
import getpass
import math
import os
import secrets
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pyotp


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-email", default=os.getenv("AMRUTAM_DEMO_ADMIN_EMAIL"))
    parser.add_argument(
        "--benchmark", action="store_true", help="measure 10 local reads/writes/bookings"
    )
    args = parser.parse_args()
    if not args.admin_email:
        parser.error("Set --admin-email or AMRUTAM_DEMO_ADMIN_EMAIL")
    admin_password = os.getenv("AMRUTAM_DEMO_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    mfa_secret = os.getenv("AMRUTAM_DEMO_MFA_SECRET")
    mfa_code = (
        pyotp.TOTP(mfa_secret).now() if mfa_secret else input("Current admin TOTP code: ").strip()
    )
    client = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=10)

    def call(method: str, path: str, expected: int, *, headers=None, **kwargs):
        response = client.request(method, path, headers=headers, **kwargs)
        if response.status_code != expected:
            raise RuntimeError(
                f"{method} {path}: expected {expected}, got {response.status_code}: {response.text}"
            )
        return response

    def show(label: str, response: httpx.Response) -> None:
        print(
            f"{label}: {response.request.method} "
            f"{response.request.url.path} -> {response.status_code}"
        )

    def register(kind: str):
        email = f"demo-{kind}-{uuid.uuid4().hex[:10]}@example.test"
        password = secrets.token_urlsafe(24)
        created = call(
            "POST",
            "/api/v1/auth/register",
            201,
            json={"email": email, "password": password, "full_name": kind.title()},
        )
        show(f"{kind} registration", created)
        logged_in = call(
            "POST", "/api/v1/auth/login", 200, json={"email": email, "password": password}
        )
        show(f"{kind} login", logged_in)
        return created.json()["id"], {"Authorization": f"Bearer {logged_in.json()['access_token']}"}

    for path in ("/health/live", "/health/ready", "/docs", "/openapi.json", "/metrics"):
        show("health/docs/metrics", call("GET", path, 200))
    doctor_id, doctor_headers = register("doctor")
    _, patient_headers = register("patient")
    _, other_headers = register("other-patient")
    show("RBAC denial", call("GET", "/api/v1/admin/analytics", 403, headers=patient_headers))
    admin_login = call(
        "POST",
        "/api/v1/auth/login",
        200,
        json={"email": args.admin_email, "password": admin_password, "mfa_code": mfa_code},
    )
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    show("admin MFA login", admin_login)
    promoted = call(
        "POST",
        f"/api/v1/doctors/admin/{doctor_id}",
        201,
        headers=admin_headers,
        json={"specialty": "Cardiology", "license_number": f"DEMO-{uuid.uuid4().hex[:10]}"},
    )
    show("doctor promotion", promoted)

    start = datetime.now(UTC) + timedelta(days=1)

    def add_slot(index: int):
        beginning = start + timedelta(hours=index)
        response = call(
            "POST",
            "/api/v1/doctors/slots",
            201,
            headers=doctor_headers,
            json={
                "starts_at": beginning.isoformat(),
                "ends_at": (beginning + timedelta(minutes=30)).isoformat(),
            },
        )
        return response.json()["id"]

    slot_id = add_slot(0)
    print("doctor availability: POST /api/v1/doctors/slots -> 201")
    show("specialty search", call("GET", "/api/v1/doctors?specialty=card&limit=1", 200))
    show(
        "availability search",
        call(
            "GET", "/api/v1/doctors", 200, params={"available_after": start.isoformat(), "limit": 1}
        ),
    )
    show("slot listing", call("GET", f"/api/v1/doctors/{doctor_id}/slots?limit=1", 200))

    booking_headers = {
        **patient_headers,
        "Idempotency-Key": f"demo-booking-{uuid.uuid4().hex[:12]}",
    }
    booking_body = {"slot_id": slot_id}
    booked = call("POST", "/api/v1/consultations", 201, headers=booking_headers, json=booking_body)
    show("booking", booked)
    replay = call("POST", "/api/v1/consultations", 201, headers=booking_headers, json=booking_body)
    assert (
        replay.json()["id"] == booked.json()["id"]
        and replay.headers.get("Idempotency-Replayed") == "true"
    )
    show("idempotent replay (same ID)", replay)
    show(
        "occupied slot rejected",
        call(
            "POST",
            "/api/v1/consultations",
            409,
            headers={**other_headers, "Idempotency-Key": f"demo-other-{uuid.uuid4().hex[:12]}"},
            json=booking_body,
        ),
    )

    race_slot = add_slot(1)

    def race_attempt(data):
        headers, key = data
        with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=10) as local:
            return local.post(
                "/api/v1/consultations",
                headers={**headers, "Idempotency-Key": key},
                json={"slot_id": race_slot},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        race = list(
            pool.map(
                race_attempt,
                [
                    (patient_headers, f"race-a-{uuid.uuid4().hex[:12]}"),
                    (other_headers, f"race-b-{uuid.uuid4().hex[:12]}"),
                ],
            )
        )
    assert sorted(race) == [201, 409], race
    print(f"concurrent booking: POST x2 same slot -> {sorted(race)} (one success)")

    consultation_id = booked.json()["id"]
    show(
        "invalid transition",
        call(
            "PATCH",
            f"/api/v1/consultations/{consultation_id}/status",
            409,
            headers=doctor_headers,
            json={"status": "completed"},
        ),
    )
    for status in ("confirmed", "in_progress", "completed"):
        show(
            f"consultation {status}",
            call(
                "PATCH",
                f"/api/v1/consultations/{consultation_id}/status",
                200,
                headers=doctor_headers,
                json={"status": status},
            ),
        )
    prescription_path = f"/api/v1/consultations/{consultation_id}/prescription"
    show(
        "prescription creation",
        call(
            "POST",
            prescription_path,
            201,
            headers=doctor_headers,
            json={"medication": "Demo medicine", "instructions": "Demo instructions"},
        ),
    )
    show("patient prescription read", call("GET", prescription_path, 200, headers=patient_headers))
    show("other patient denied", call("GET", prescription_path, 403, headers=other_headers))
    show(
        "consultation filter",
        call("GET", "/api/v1/consultations?status=completed&limit=1", 200, headers=patient_headers),
    )
    show(
        "admin analytics", call("GET", "/api/v1/admin/analytics?days=7", 200, headers=admin_headers)
    )
    show("audit trail", call("GET", "/api/v1/admin/audit-logs?limit=5", 200, headers=admin_headers))
    show("metrics after requests", call("GET", "/metrics", 200))

    if args.benchmark:
        samples = {"read_doctors": [], "write_profile": [], "write_booking": []}
        for i in range(10):
            began = time.perf_counter()
            call("GET", "/api/v1/doctors?specialty=card&limit=10", 200)
            samples["read_doctors"].append((time.perf_counter() - began) * 1000)
            began = time.perf_counter()
            call(
                "PATCH",
                "/api/v1/auth/profile",
                200,
                headers=patient_headers,
                json={"full_name": f"Benchmark {i}"},
            )
            samples["write_profile"].append((time.perf_counter() - began) * 1000)
            fresh_slot = add_slot(i + 2)
            began = time.perf_counter()
            call(
                "POST",
                "/api/v1/consultations",
                201,
                headers={**patient_headers, "Idempotency-Key": f"bench-{uuid.uuid4().hex[:20]}"},
                json={"slot_id": fresh_slot},
            )
            samples["write_booking"].append((time.perf_counter() - began) * 1000)
        print(
            "LOCAL BENCHMARK: 10 sequential HTTP requests per operation; "
            "warm local Docker; excludes setup and slot creation; milliseconds"
        )
        for name, values in samples.items():
            print(
                f"{name}: n={len(values)} median={statistics.median(values):.1f} "
                f"p95_nearest_rank={percentile(values, 0.95):.1f} max={max(values):.1f}"
            )
        print("These numbers do not validate production throughput, p95 SLOs, or availability.")
    print("DEMO PASSED. Video recording remains separate.")


if __name__ == "__main__":
    main()
