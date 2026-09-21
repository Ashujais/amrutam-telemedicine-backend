import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet

from app.config import get_settings

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False


def token(user_id: uuid.UUID, kind: str, version: int) -> str:
    settings = get_settings()
    duration = (
        timedelta(minutes=settings.access_token_minutes)
        if kind == "access"
        else timedelta(days=settings.refresh_token_days)
    )
    return jwt.encode(
        {
            "sub": str(user_id),
            "typ": kind,
            "ver": version,
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(UTC) + duration,
            "iat": datetime.now(UTC),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_token(value: str, kind: str) -> dict:
    payload = jwt.decode(
        value,
        get_settings().jwt_secret,
        algorithms=["HS256"],
        options={"require": ["sub", "exp", "typ", "jti", "ver"]},
    )
    if payload["typ"] != kind:
        raise jwt.InvalidTokenError("Wrong token type")
    return payload


def encrypt_mfa(secret: str) -> bytes:
    return Fernet(get_settings().mfa_encryption_key.encode()).encrypt(secret.encode())


def decrypt_mfa(ciphertext: bytes) -> str:
    return Fernet(get_settings().mfa_encryption_key.encode()).decrypt(ciphertext).decode()


def verify_mfa(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def hash_request(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
