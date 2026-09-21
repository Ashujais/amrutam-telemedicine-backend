import uuid
from datetime import UTC, datetime

import pyotp
import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.dependencies import current_user
from app.models import Profile, Role, User
from app.schemas import (
    LoginIn,
    MfaEnabledOut,
    MfaSetupOut,
    ProfileIn,
    ProfileOut,
    RefreshIn,
    RegisterIn,
    Tokens,
    UserOut,
)
from app.security import (
    decode_token,
    decrypt_mfa,
    encrypt_mfa,
    hash_password,
    token,
    verify_mfa,
    verify_password,
)
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def rate_limit(bucket: str, request: Request, limit: int, window: int) -> None:
    client = request.client.host if request.client else "unknown"
    try:
        cache = redis.Redis.from_url(get_settings().redis_url, socket_timeout=1)
        key = f"rate:{bucket}:{client}"
        count = cache.eval(
            "local n=redis.call('INCR', KEYS[1]); "
            "if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]); end; return n",
            1,
            key,
            window,
        )
        if count > limit:
            raise HTTPException(429, "Rate limit exceeded")
    except redis.RedisError:
        raise HTTPException(503, "Rate limiter unavailable") from None


@router.post("/register", response_model=UserOut, status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("register", request, 10, 3600)
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    try:
        db.add(user)
        db.flush()
        db.add(Profile(user_id=user.id, full_name=body.full_name))
        audit(db, user, "user.registered", "user", user.id, request.state.request_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email already registered") from None
    return user


@router.post("/login", response_model=Tokens)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("login", request, 10, 300)
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if (
        not user
        or not user.is_active
        or user.deleted_at
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(401, "Invalid credentials")
    if user.role == Role.admin and not user.mfa_enabled:
        raise HTTPException(403, "Administrator MFA required")
    if user.mfa_enabled:
        if not body.mfa_code or not verify_mfa(
            decrypt_mfa(user.mfa_secret_encrypted), body.mfa_code
        ):
            raise HTTPException(401, "Invalid credentials")
    audit(db, user, "auth.login", "user", user.id, request.state.request_id)
    db.commit()
    return Tokens(
        access_token=token(user.id, "access", user.token_version),
        refresh_token=token(user.id, "refresh", user.token_version),
    )


@router.post("/refresh", response_model=Tokens)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("refresh", request, 30, 300)
    import jwt

    try:
        payload = decode_token(body.refresh_token, "refresh")
        user = db.get(User, uuid.UUID(payload["sub"]))
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise HTTPException(401, "Invalid refresh token") from None
    if not user or not user.is_active or user.deleted_at or user.token_version != payload["ver"]:
        raise HTTPException(401, "Invalid refresh token")
    ttl = max(1, int(payload["exp"] - datetime.now(UTC).timestamp()))
    try:
        cache = redis.Redis.from_url(get_settings().redis_url, socket_timeout=1)
        if not cache.set(f"refresh-used:{payload['jti']}", "1", ex=ttl, nx=True):
            raise HTTPException(401, "Refresh token already used")
    except redis.RedisError:
        raise HTTPException(503, "Refresh store unavailable") from None
    audit(db, user, "auth.refresh", "user", user.id, request.state.request_id)
    db.commit()
    return Tokens(
        access_token=token(user.id, "access", user.token_version),
        refresh_token=token(user.id, "refresh", user.token_version),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.get("/profile", response_model=ProfileOut)
def get_profile(user: User = Depends(current_user), db: Session = Depends(get_db)):
    profile = db.get(Profile, user.id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return profile


@router.patch("/profile", response_model=ProfileOut)
def update_profile(
    body: ProfileIn,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, user.id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    profile.full_name = body.full_name
    audit(db, user, "profile.updated", "profile", user.id, request.state.request_id)
    db.commit()
    return profile


@router.post("/logout", status_code=204)
def logout(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.token_version += 1
    audit(db, user, "auth.logout", "user", user.id, request.state.request_id)
    db.commit()


@router.post("/mfa/setup", response_model=MfaSetupOut)
def mfa_setup(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    if user.mfa_enabled:
        raise HTTPException(409, "MFA already enabled")
    secret = pyotp.random_base32()
    user.mfa_secret_encrypted = encrypt_mfa(secret)
    audit(db, user, "auth.mfa_setup", "user", user.id, request.state.request_id)
    db.commit()
    return {
        "secret": secret,
        "otpauth_url": pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Amrutam"),
    }


@router.post("/mfa/enable", response_model=MfaEnabledOut)
def mfa_enable(
    code: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    if user.mfa_enabled:
        raise HTTPException(409, "MFA already enabled")
    if not user.mfa_secret_encrypted or not verify_mfa(
        decrypt_mfa(user.mfa_secret_encrypted), code
    ):
        raise HTTPException(400, "Invalid MFA code")
    user.mfa_enabled = True
    user.token_version += 1
    audit(db, user, "auth.mfa_enabled", "user", user.id, request.state.request_id)
    db.commit()
    return {"enabled": True}


@router.delete("/me", status_code=204)
def delete_me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.is_active = False
    user.deleted_at = datetime.now(UTC)
    user.token_version += 1
    audit(db, user, "user.deleted", "user", user.id, request.state.request_id)
    db.commit()
