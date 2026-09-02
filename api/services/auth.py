import time
import uuid
from datetime import timedelta

import jwt
from fastapi import Cookie, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import User
from api.db.session import get_db
from api.errors import ApiError
from api.services.secrets import get_secret

SESSION_COOKIE_NAME = "session"
SESSION_TYP = "session"
INSTALL_STATE_TYP = "install_state"
SESSION_TTL = timedelta(days=14)
INSTALL_STATE_TTL = timedelta(minutes=10)

JWT_ALGORITHM = "HS256"


class InvalidTokenTypeError(jwt.InvalidTokenError):
    def __init__(self, expected: str, actual: str | None) -> None:
        super().__init__(f"expected typ={expected!r}, got {actual!r}")


async def _get_signing_key() -> str:
    return await get_secret("SESSION_SECRET_KEY")


async def _create_jwt(user_id: uuid.UUID, *, typ: str, ttl: timedelta) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "typ": typ,
        "iat": now,
        "exp": now + int(ttl.total_seconds()),
    }
    key = await _get_signing_key()
    return jwt.encode(payload, key, algorithm=JWT_ALGORITHM)


async def _verify_jwt(token: str, *, expected_typ: str) -> uuid.UUID:
    key = await _get_signing_key()
    payload = jwt.decode(token, key, algorithms=[JWT_ALGORITHM])
    typ = payload.get("typ")
    if typ != expected_typ:
        raise InvalidTokenTypeError(expected_typ, typ)
    return uuid.UUID(payload["sub"])


async def create_session_jwt(user_id: uuid.UUID) -> str:
    return await _create_jwt(user_id, typ=SESSION_TYP, ttl=SESSION_TTL)


async def verify_session_jwt(token: str) -> uuid.UUID:
    return await _verify_jwt(token, expected_typ=SESSION_TYP)


async def create_install_state_jwt(user_id: uuid.UUID) -> str:
    return await _create_jwt(user_id, typ=INSTALL_STATE_TYP, ttl=INSTALL_STATE_TTL)


async def verify_install_state_jwt(token: str) -> uuid.UUID:
    return await _verify_jwt(token, expected_typ=INSTALL_STATE_TYP)


async def get_current_user(
    session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if session is None:
        raise ApiError(401, "not_authenticated", "Требуется вход")
    try:
        user_id = await verify_session_jwt(session)
    except jwt.InvalidTokenError:
        raise ApiError(401, "invalid_session", "Сессия недействительна") from None
    user = await db.get(User, user_id)
    if user is None:
        raise ApiError(401, "invalid_session", "Пользователь не найден") from None
    return user
