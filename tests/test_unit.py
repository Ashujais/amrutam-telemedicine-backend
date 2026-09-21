import uuid

from app.models import Consultation, ConsultationStatus, Role, User
from app.security import decode_token, hash_password, token, verify_password
from app.services import TRANSITIONS, can_view_consultation


def test_password_and_token():
    password_hash = hash_password("a-long-demo-password")
    assert verify_password("a-long-demo-password", password_hash)
    assert not verify_password("wrong", password_hash)
    user_id = uuid.uuid4()
    access = token(user_id, "access", 2)
    assert decode_token(access, "access")["sub"] == str(user_id)


def test_transitions_and_ownership():
    assert ConsultationStatus.confirmed in TRANSITIONS[ConsultationStatus.scheduled]
    assert ConsultationStatus.completed not in TRANSITIONS[ConsultationStatus.scheduled]
    patient = User(id=uuid.uuid4(), role=Role.patient)
    stranger = User(id=uuid.uuid4(), role=Role.patient)
    consultation = Consultation(patient_id=patient.id, doctor_id=uuid.uuid4())
    assert can_view_consultation(consultation, patient)
    assert not can_view_consultation(consultation, stranger)
