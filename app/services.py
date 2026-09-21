import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    AvailabilitySlot,
    Consultation,
    ConsultationStatus,
    IdempotencyRecord,
    User,
    now,
)
from app.security import hash_request


def audit(
    db: Session,
    actor: User | None,
    action: str,
    resource: str,
    resource_id: uuid.UUID | str,
    request_id: str | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=actor.id if actor else None,
            action=action,
            resource=resource,
            resource_id=str(resource_id),
            request_id=request_id,
            details=details or {},
        )
    )


TRANSITIONS = {
    ConsultationStatus.scheduled: {ConsultationStatus.confirmed, ConsultationStatus.cancelled},
    ConsultationStatus.confirmed: {ConsultationStatus.in_progress, ConsultationStatus.cancelled},
    ConsultationStatus.in_progress: {ConsultationStatus.completed},
    ConsultationStatus.completed: set(),
    ConsultationStatus.cancelled: set(),
}


def book(
    db: Session, patient: User, slot_id: uuid.UUID, key: str, request_id: str | None
) -> tuple[Consultation, bool]:
    fingerprint = hash_request(str(slot_id))
    existing = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == patient.id,
            IdempotencyRecord.scope == "booking",
            IdempotencyRecord.key == key,
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise HTTPException(409, "Idempotency key reused with different request")
        return db.get(Consultation, uuid.UUID(existing.resource_id)), True
    slot = db.scalar(
        select(AvailabilitySlot).where(AvailabilitySlot.id == slot_id).with_for_update()
    )
    if slot is None:
        raise HTTPException(404, "Slot not found")
    if slot.is_booked or slot.starts_at <= now():
        raise HTTPException(409, "Slot unavailable")
    slot.is_booked = True
    consultation = Consultation(slot_id=slot.id, patient_id=patient.id, doctor_id=slot.doctor_id)
    db.add(consultation)
    db.flush()
    db.add(
        IdempotencyRecord(
            actor_id=patient.id,
            scope="booking",
            key=key,
            request_hash=fingerprint,
            resource_id=str(consultation.id),
        )
    )
    audit(db, patient, "booking.created", "consultation", consultation.id, request_id)
    return consultation, False


def transition(
    db: Session,
    consultation_id: uuid.UUID,
    actor: User,
    status: ConsultationStatus,
    request_id: str | None,
) -> Consultation:
    consultation = db.scalar(
        select(Consultation).where(Consultation.id == consultation_id).with_for_update()
    )
    if consultation is None:
        raise HTTPException(404, "Consultation not found")
    if actor.role.value == "patient":
        if actor.id != consultation.patient_id or status != ConsultationStatus.cancelled:
            raise HTTPException(403, "Transition forbidden")
    elif actor.role.value == "doctor" and actor.id != consultation.doctor_id:
        raise HTTPException(403, "Transition forbidden")
    if status not in TRANSITIONS[consultation.status]:
        raise HTTPException(409, "Invalid consultation transition")
    consultation.status = status
    if status == ConsultationStatus.cancelled:
        consultation.cancelled_at = now()
    audit(
        db,
        actor,
        "consultation.transition",
        "consultation",
        consultation.id,
        request_id,
        {"status": status.value},
    )
    return consultation


def can_view_consultation(consultation: Consultation, actor: User) -> bool:
    return actor.role.value == "admin" or actor.id in (
        consultation.patient_id,
        consultation.doctor_id,
    )


def can_view_prescription(consultation: Consultation, actor: User) -> bool:
    return can_view_consultation(consultation, actor)
