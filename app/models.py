import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def now() -> datetime:
    return datetime.now(UTC)


class Role(enum.StrEnum):
    patient = "patient"
    doctor = "doctor"
    admin = "admin"


class ConsultationStatus(enum.StrEnum):
    scheduled = "scheduled"
    confirmed = "confirmed"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class PaymentStatus(enum.StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base, Timestamps):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.patient)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    profile: Mapped["Profile"] = relationship(back_populates="user", uselist=False)


class Profile(Base, Timestamps):
    __tablename__ = "profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    user: Mapped[User] = relationship(back_populates="profile")


class Doctor(Base, Timestamps):
    __tablename__ = "doctors"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    specialty: Mapped[str] = mapped_column(String(80), index=True)
    license_number: Mapped[str] = mapped_column(String(80), unique=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    user: Mapped[User] = relationship()


class AvailabilitySlot(Base, Timestamps):
    __tablename__ = "availability_slots"
    __table_args__ = (
        UniqueConstraint("doctor_id", "starts_at", name="uq_doctor_slot_start"),
        CheckConstraint("ends_at > starts_at", name="ck_slot_time"),
        Index("ix_slot_doctor_open_start", "doctor_id", "is_booked", "starts_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doctors.user_id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_booked: Mapped[bool] = mapped_column(Boolean, default=False)


class Consultation(Base, Timestamps):
    __tablename__ = "consultations"
    __table_args__ = (
        UniqueConstraint("slot_id", name="uq_consultation_slot"),
        Index("ix_consultation_patient_created", "patient_id", "created_at"),
        Index("ix_consultation_doctor_created", "doctor_id", "created_at"),
        Index("ix_consultation_status_created", "status", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("availability_slots.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doctors.user_id"))
    status: Mapped[ConsultationStatus] = mapped_column(
        Enum(ConsultationStatus, name="consultation_status"), default=ConsultationStatus.scheduled
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot: Mapped[AvailabilitySlot] = relationship()


class Prescription(Base, Timestamps):
    __tablename__ = "prescriptions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consultation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consultations.id"), unique=True)
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doctors.user_id"))
    medication: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)


class Payment(Base, Timestamps):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount_minor >= 0", name="ck_payment_amount"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consultation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consultations.id"), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.pending
    )
    provider_reference: Mapped[str | None] = mapped_column(String(120), unique=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_resource", "resource", "resource_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    resource: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(80))
    request_id: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("actor_id", "scope", "key", name="uq_idempotency_actor_scope_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
