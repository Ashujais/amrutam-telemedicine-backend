"""One-time administrator bootstrap. ADMIN_EMAIL and ADMIN_PASSWORD come from environment."""

import os
import sys

import pyotp
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Profile, Role, User
from app.security import encrypt_mfa, hash_password


def main() -> None:
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or len(password) < 12:
        sys.exit("Set ADMIN_EMAIL and ADMIN_PASSWORD (at least 12 characters)")
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email)):
            sys.exit("Account already exists")
        secret = pyotp.random_base32()
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=Role.admin,
            mfa_secret_encrypted=encrypt_mfa(secret),
            mfa_enabled=True,
        )
        db.add(user)
        db.flush()
        db.add(Profile(user_id=user.id, full_name=email.split("@")[0]))
        db.commit()
    print(pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Amrutam"))
    print("Save the TOTP enrollment URI now; it is shown only once.")


if __name__ == "__main__":
    main()
