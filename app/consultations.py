import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import current_user, require_role
from app.models import Consultation, ConsultationStatus, IdempotencyRecord, Prescription, Role, User
from app.schemas import BookingIn, ConsultationOut, Page, PrescriptionIn, PrescriptionOut, StatusIn
from app.security import hash_request
from app.services import audit, book, can_view_consultation, can_view_prescription, transition

router = APIRouter(prefix="/consultations", tags=["consultations"])


@router.post("", response_model=ConsultationOut, status_code=201)
def create_booking(
    body: BookingIn,
    request: Request,
    idempotency_key: str = Header(min_length=8, max_length=128),
    patient: User = Depends(require_role(Role.patient)),
    db: Session = Depends(get_db),
):
    try:
        consultation, replay = book(
            db, patient, body.slot_id, idempotency_key, request.state.request_id
        )
        db.commit()
        if replay:
            request.state.replayed = True
        return consultation
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_id == patient.id,
                IdempotencyRecord.scope == "booking",
                IdempotencyRecord.key == idempotency_key,
            )
        )
        if existing and existing.request_hash == hash_request(str(body.slot_id)):
            request.state.replayed = True
            return db.get(Consultation, uuid.UUID(existing.resource_id))
        raise HTTPException(409, "Slot unavailable or idempotency conflict") from None


@router.get("", response_model=Page[ConsultationOut])
def list_consultations(
    status: ConsultationStatus | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    filters = []
    if actor.role == Role.patient:
        filters.append(Consultation.patient_id == actor.id)
    elif actor.role == Role.doctor:
        filters.append(Consultation.doctor_id == actor.id)
    if status:
        filters.append(Consultation.status == status)
    if created_after:
        if created_after.tzinfo is None:
            raise HTTPException(422, "created_after requires timezone")
        filters.append(Consultation.created_at >= created_after)
    if created_before:
        if created_before.tzinfo is None:
            raise HTTPException(422, "created_before requires timezone")
        filters.append(Consultation.created_at <= created_before)
    total = db.scalar(select(func.count()).select_from(Consultation).where(*filters))
    items = db.scalars(
        select(Consultation)
        .where(*filters)
        .order_by(Consultation.created_at.desc(), Consultation.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [ConsultationOut.model_validate(x) for x in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{consultation_id}", response_model=ConsultationOut)
def get_consultation(
    consultation_id: uuid.UUID, actor: User = Depends(current_user), db: Session = Depends(get_db)
):
    consultation = db.get(Consultation, consultation_id)
    if not consultation:
        raise HTTPException(404, "Consultation not found")
    if not can_view_consultation(consultation, actor):
        raise HTTPException(403, "Access denied")
    return consultation


@router.patch("/{consultation_id}/status", response_model=ConsultationOut)
def update_status(
    consultation_id: uuid.UUID,
    body: StatusIn,
    request: Request,
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    consultation = transition(db, consultation_id, actor, body.status, request.state.request_id)
    db.commit()
    return consultation


@router.post("/{consultation_id}/prescription", response_model=PrescriptionOut, status_code=201)
def create_prescription(
    consultation_id: uuid.UUID,
    body: PrescriptionIn,
    request: Request,
    doctor: User = Depends(require_role(Role.doctor)),
    db: Session = Depends(get_db),
):
    consultation = db.scalar(
        select(Consultation).where(Consultation.id == consultation_id).with_for_update()
    )
    if not consultation:
        raise HTTPException(404, "Consultation not found")
    if consultation.doctor_id != doctor.id:
        raise HTTPException(403, "Access denied")
    if consultation.status != ConsultationStatus.completed:
        raise HTTPException(409, "Consultation is not completed")
    existing = db.scalar(
        select(Prescription).where(Prescription.consultation_id == consultation_id)
    )
    if existing:
        raise HTTPException(409, "Prescription already exists")
    prescription = Prescription(
        consultation_id=consultation_id,
        doctor_id=doctor.id,
        medication=body.medication,
        instructions=body.instructions,
    )
    db.add(prescription)
    db.flush()
    audit(
        db,
        doctor,
        "prescription.created",
        "prescription",
        prescription.id,
        request.state.request_id,
    )
    db.commit()
    return prescription


@router.get("/{consultation_id}/prescription", response_model=PrescriptionOut)
def get_prescription(
    consultation_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    consultation = db.get(Consultation, consultation_id)
    if not consultation:
        raise HTTPException(404, "Consultation not found")
    if not can_view_prescription(consultation, actor):
        raise HTTPException(403, "Access denied")
    prescription = db.scalar(
        select(Prescription).where(Prescription.consultation_id == consultation_id)
    )
    if not prescription:
        raise HTTPException(404, "Prescription not found")
    audit(
        db, actor, "prescription.viewed", "prescription", prescription.id, request.state.request_id
    )
    db.commit()
    return prescription
