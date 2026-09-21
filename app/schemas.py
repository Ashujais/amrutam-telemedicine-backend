import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ConsultationStatus, PaymentStatus, Role


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)


class LoginIn(BaseModel):
    email: str
    password: str
    mfa_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class RefreshIn(BaseModel):
    refresh_token: str


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    role: Role
    is_active: bool
    mfa_enabled: bool


class ProfileIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)


class ProfileOut(ORMModel):
    user_id: uuid.UUID
    full_name: str


class DoctorIn(BaseModel):
    specialty: str = Field(min_length=2, max_length=80)
    license_number: str = Field(min_length=2, max_length=80)


class DoctorOut(ORMModel):
    user_id: uuid.UUID
    specialty: str
    is_verified: bool


class SlotIn(BaseModel):
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timezone is required")
        return value


class SlotOut(ORMModel):
    id: uuid.UUID
    doctor_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    is_booked: bool


class BookingIn(BaseModel):
    slot_id: uuid.UUID


class ConsultationOut(ORMModel):
    id: uuid.UUID
    slot_id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    status: ConsultationStatus
    created_at: datetime


class StatusIn(BaseModel):
    status: ConsultationStatus


class PrescriptionIn(BaseModel):
    medication: str = Field(min_length=1, max_length=4000)
    instructions: str = Field(min_length=1, max_length=4000)


class PrescriptionOut(ORMModel):
    id: uuid.UUID
    consultation_id: uuid.UUID
    doctor_id: uuid.UUID
    medication: str
    instructions: str
    created_at: datetime


class PaymentIn(BaseModel):
    amount_minor: int = Field(ge=0)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")


class PaymentStatusIn(BaseModel):
    status: PaymentStatus
    provider_reference: str | None = Field(default=None, max_length=120)


class PaymentOut(ORMModel):
    id: uuid.UUID
    consultation_id: uuid.UUID
    amount_minor: int
    currency: str
    status: PaymentStatus


class Page[T](BaseModel):
    items: list[T]
    limit: int
    offset: int
    total: int


class ErrorOut(BaseModel):
    error: str
    request_id: str


class AuditOut(ORMModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    resource: str
    resource_id: str
    request_id: str | None
    details: dict
    created_at: datetime


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_url: str


class MfaEnabledOut(BaseModel):
    enabled: bool


class AnalyticsTotals(BaseModel):
    users: int
    doctors: int
    consultations: int
    completed: int
    cancelled: int
    revenue_minor: int


class DailyCount(BaseModel):
    date: date
    count: int


class AnalyticsOut(BaseModel):
    totals: AnalyticsTotals
    daily_consultations: list[DailyCount]
