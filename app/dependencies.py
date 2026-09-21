import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Role, User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(credentials.credentials, "access")
        user = db.get(User, uuid.UUID(payload["sub"]))
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise HTTPException(401, "Invalid token") from None
    if (
        user is None
        or not user.is_active
        or user.deleted_at
        or user.token_version != payload["ver"]
    ):
        raise HTTPException(401, "Invalid token")
    return user


def require_role(*roles: Role) -> Callable:
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Insufficient role")
        return user

    return dependency
