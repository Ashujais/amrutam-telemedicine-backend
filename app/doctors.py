import uuid
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import require_role
from app.models import AvailabilitySlot, Doctor, Role, User, now
from app.schemas import DoctorIn, DoctorOut, Page, SlotIn, SlotOut
from app.services import audit

router = APIRouter(prefix="/doctors", tags=["doctors"])


@router.post("/admin/{user_id}", response_model=DoctorOut, status_code=201)
def create_doctor(
    user_id: uuid.UUID,
    body: DoctorIn,
    request: Request,
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(404, "User not found")
    if db.get(Doctor, user_id):
        raise HTTPException(409, "Doctor already exists")
    user.role = Role.doctor
    doctor = Doctor(
        user_id=user_id,
        specialty=body.specialty,
        license_number=body.license_number,
        is_verified=True,
    )
    db.add(doctor)
    audit(db, admin, "doctor.created", "doctor", user_id, request.state.request_id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "License already registered") from None
    return doctor


@router.get("", response_model=Page[DoctorOut])
def search_doctors(
    specialty: str | None = Query(default=None, max_length=80),
    available_after: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    filters = [Doctor.is_verified.is_(True), User.is_active.is_(True), User.deleted_at.is_(None)]
    if specialty:
        term = specialty.replace("%", "\\%").replace("_", "\\_")
        filters.append(Doctor.specialty.ilike(f"%{term}%", escape="\\"))
    if available_after:
        from datetime import datetime

        try:
            after = datetime.fromisoformat(available_after)
            if after.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise HTTPException(
                422, "available_after must be an ISO timestamp with timezone"
            ) from None
        filters.append(
            select(AvailabilitySlot.id)
            .where(
                AvailabilitySlot.doctor_id == Doctor.user_id,
                AvailabilitySlot.is_booked.is_(False),
                AvailabilitySlot.starts_at >= after,
            )
            .exists()
        )
    base = select(Doctor).join(User, User.id == Doctor.user_id).where(*filters)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    items = db.scalars(base.order_by(Doctor.user_id).limit(limit).offset(offset)).all()
    return {
        "items": [DoctorOut.model_validate(x) for x in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{doctor_id}/slots", response_model=Page[SlotOut])
def list_slots(
    doctor_id: uuid.UUID,
    after: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    if not db.get(Doctor, doctor_id):
        raise HTTPException(404, "Doctor not found")
    filters = [
        AvailabilitySlot.doctor_id == doctor_id,
        AvailabilitySlot.is_booked.is_(False),
        AvailabilitySlot.starts_at > now(),
    ]
    if after:
        from datetime import datetime

        try:
            value = datetime.fromisoformat(after)
            if value.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "after must be an ISO timestamp with timezone") from None
        filters.append(AvailabilitySlot.starts_at >= value)
    total = db.scalar(select(func.count()).select_from(AvailabilitySlot).where(*filters))
    items = db.scalars(
        select(AvailabilitySlot)
        .where(*filters)
        .order_by(AvailabilitySlot.starts_at)
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [SlotOut.model_validate(x) for x in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/slots", response_model=SlotOut, status_code=201)
def add_slot(
    body: SlotIn,
    request: Request,
    doctor_user: User = Depends(require_role(Role.doctor)),
    db: Session = Depends(get_db),
):
    doctor = db.get(Doctor, doctor_user.id)
    if not doctor or not doctor.is_verified:
        raise HTTPException(403, "Doctor not verified")
    if body.starts_at <= now() or body.ends_at <= body.starts_at:
        raise HTTPException(422, "Slot must be in the future with a positive duration")
    if (body.ends_at - body.starts_at).total_seconds() > 7200:
        raise HTTPException(422, "Slot duration cannot exceed two hours")
    slot = AvailabilitySlot(
        doctor_id=doctor_user.id,
        starts_at=body.starts_at.astimezone(UTC),
        ends_at=body.ends_at.astimezone(UTC),
    )
    try:
        db.add(slot)
        db.flush()
        audit(
            db, doctor_user, "slot.created", "availability_slot", slot.id, request.state.request_id
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Overlapping slot") from None
    return slot
