import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import require_role
from app.models import (
    AuditLog,
    Consultation,
    ConsultationStatus,
    IdempotencyRecord,
    Payment,
    PaymentStatus,
    Role,
    User,
)
from app.schemas import AnalyticsOut, AuditOut, Page, PaymentIn, PaymentOut, PaymentStatusIn
from app.security import hash_request
from app.services import audit

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/analytics", response_model=AnalyticsOut)
def analytics(
    days: int = Query(7, ge=1, le=90),
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    since = date.today() - timedelta(days=days - 1)
    totals = {
        "users": db.scalar(select(func.count(User.id)).where(User.deleted_at.is_(None))),
        "doctors": db.scalar(
            select(func.count(User.id)).where(User.role == Role.doctor, User.deleted_at.is_(None))
        ),
        "consultations": db.scalar(select(func.count(Consultation.id))),
        "completed": db.scalar(
            select(func.count(Consultation.id)).where(
                Consultation.status == ConsultationStatus.completed
            )
        ),
        "cancelled": db.scalar(
            select(func.count(Consultation.id)).where(
                Consultation.status == ConsultationStatus.cancelled
            )
        ),
        "revenue_minor": db.scalar(
            select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
                Payment.status == PaymentStatus.succeeded
            )
        ),
    }
    rows = db.execute(
        select(func.date(Consultation.created_at), func.count())
        .where(
            Consultation.created_at >= since,
        )
        .group_by(func.date(Consultation.created_at))
        .order_by(func.date(Consultation.created_at))
    ).all()
    return {
        "totals": totals,
        "daily_consultations": [{"date": str(day), "count": count} for day, count in rows],
    }


@router.post("/consultations/{consultation_id}/payment", response_model=PaymentOut, status_code=201)
def create_payment(
    consultation_id: uuid.UUID,
    body: PaymentIn,
    request: Request,
    idempotency_key: str = Header(min_length=8, max_length=128),
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    consultation = db.scalar(
        select(Consultation).where(Consultation.id == consultation_id).with_for_update()
    )
    if not consultation:
        raise HTTPException(404, "Consultation not found")
    fingerprint = hash_request(str(consultation_id), str(body.amount_minor), body.currency)
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == admin.id,
            IdempotencyRecord.scope == "payment-create",
            IdempotencyRecord.key == idempotency_key,
        )
    )
    if record:
        if record.request_hash != fingerprint:
            raise HTTPException(409, "Idempotency key reused")
        return db.get(Payment, uuid.UUID(record.resource_id))
    payment = db.scalar(select(Payment).where(Payment.consultation_id == consultation_id))
    if payment:
        raise HTTPException(409, "Payment already exists")
    payment = Payment(
        consultation_id=consultation_id, amount_minor=body.amount_minor, currency=body.currency
    )
    db.add(payment)
    db.flush()
    db.add(
        IdempotencyRecord(
            actor_id=admin.id,
            scope="payment-create",
            key=idempotency_key,
            request_hash=fingerprint,
            resource_id=str(payment.id),
        )
    )
    audit(db, admin, "payment.created", "payment", payment.id, request.state.request_id)
    db.commit()
    return payment


@router.patch("/payments/{payment_id}", response_model=PaymentOut)
def update_payment(
    payment_id: uuid.UUID,
    body: PaymentStatusIn,
    request: Request,
    idempotency_key: str = Header(min_length=8, max_length=128),
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())
    if not payment:
        raise HTTPException(404, "Payment not found")
    fingerprint = hash_request(str(payment_id), body.status.value, body.provider_reference or "")
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == admin.id,
            IdempotencyRecord.scope == "payment-status",
            IdempotencyRecord.key == idempotency_key,
        )
    )
    if record:
        if record.request_hash != fingerprint:
            raise HTTPException(409, "Idempotency key reused")
        return payment
    transitions = {
        PaymentStatus.pending: {PaymentStatus.succeeded, PaymentStatus.failed},
        PaymentStatus.succeeded: {PaymentStatus.refunded},
        PaymentStatus.failed: set(),
        PaymentStatus.refunded: set(),
    }
    if body.status not in transitions[payment.status]:
        raise HTTPException(409, "Invalid payment transition")
    payment.status = body.status
    payment.provider_reference = body.provider_reference
    db.add(
        IdempotencyRecord(
            actor_id=admin.id,
            scope="payment-status",
            key=idempotency_key,
            request_hash=fingerprint,
            resource_id=str(payment.id),
        )
    )
    audit(
        db,
        admin,
        "payment.transition",
        "payment",
        payment.id,
        request.state.request_id,
        {"status": body.status.value},
    )
    db.commit()
    return payment


@router.get("/audit-logs", response_model=Page[AuditOut])
def list_audit_logs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count(AuditLog.id)))
    rows = db.scalars(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc(), AuditLog.id)
        .limit(limit)
        .offset(offset)
    ).all()
    audit(db, admin, "audit.viewed", "audit_log", "page", request.state.request_id)
    db.commit()
    return {"items": rows, "total": total, "limit": limit, "offset": offset}
