import time
import uuid

import jwt
import pytest

from api.services import auth

SIGNING_KEY = "test-signing-key"


@pytest.fixture(autouse=True)
def _fixed_signing_key(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_signing_key() -> str:
        return SIGNING_KEY

    monkeypatch.setattr(auth, "_get_signing_key", fake_get_signing_key)


async def test_create_and_verify_session_jwt_roundtrip() -> None:
    user_id = uuid.uuid4()
    token = await auth.create_session_jwt(user_id)

    assert await auth.verify_session_jwt(token) == user_id


async def test_session_jwt_has_expected_typ_and_exp() -> None:
    user_id = uuid.uuid4()
    before = int(time.time())
    token = await auth.create_session_jwt(user_id)

    payload = jwt.decode(token, SIGNING_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert payload["typ"] == auth.SESSION_TYP
    assert payload["iat"] >= before
    assert payload["exp"] - payload["iat"] == int(auth.SESSION_TTL.total_seconds())


async def test_expired_session_jwt_is_rejected() -> None:
    now = int(time.time())
    expired_payload = {
        "sub": str(uuid.uuid4()),
        "typ": auth.SESSION_TYP,
        "iat": now - 100,
        "exp": now - 1,
    }
    token = jwt.encode(expired_payload, SIGNING_KEY, algorithm=auth.JWT_ALGORITHM)

    with pytest.raises(jwt.ExpiredSignatureError):
        await auth.verify_session_jwt(token)


async def test_tampered_signature_is_rejected() -> None:
    now = int(time.time())
    payload = {"sub": str(uuid.uuid4()), "typ": auth.SESSION_TYP, "iat": now, "exp": now + 3600}
    token = jwt.encode(payload, "wrong-key", algorithm=auth.JWT_ALGORITHM)

    with pytest.raises(jwt.InvalidSignatureError):
        await auth.verify_session_jwt(token)


async def test_install_state_jwt_roundtrip() -> None:
    user_id = uuid.uuid4()
    token = await auth.create_install_state_jwt(user_id)

    assert await auth.verify_install_state_jwt(token) == user_id


async def test_install_state_jwt_has_short_exp() -> None:
    token = await auth.create_install_state_jwt(uuid.uuid4())

    payload = jwt.decode(token, SIGNING_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert payload["exp"] - payload["iat"] == int(auth.INSTALL_STATE_TTL.total_seconds())


async def test_session_jwt_rejected_as_install_state() -> None:
    token = await auth.create_session_jwt(uuid.uuid4())

    with pytest.raises(auth.InvalidTokenTypeError):
        await auth.verify_install_state_jwt(token)


async def test_install_state_jwt_rejected_as_session() -> None:
    token = await auth.create_install_state_jwt(uuid.uuid4())

    with pytest.raises(auth.InvalidTokenTypeError):
        await auth.verify_session_jwt(token)
