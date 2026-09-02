import os
import secrets as pysecrets
import uuid
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import User
from api.db.session import get_db
from api.errors import ApiError
from api.services.auth import SESSION_COOKIE_NAME, SESSION_TTL, create_session_jwt, get_current_user
from api.services.secrets import get_secret

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"
OAUTH_STATE_COOKIE_NAME = "oauth_state"
OAUTH_STATE_TTL_SECONDS = 600


def _is_local() -> bool:
    return os.environ.get("APP_ENV") == "local"


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    client_id = os.environ["GITHUB_APP_CLIENT_ID"]
    csrf_state = pysecrets.token_urlsafe(32)
    redirect_uri = str(request.url_for("github_oauth_callback"))

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": csrf_state,
        }
    )
    response = RedirectResponse(url=f"{GITHUB_AUTHORIZE_URL}?{query}", status_code=302)
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        csrf_state,
        httponly=True,
        secure=not _is_local(),
        samesite="lax",
        max_age=OAUTH_STATE_TTL_SECONDS,
        path="/",
    )
    return response


@router.get("/callback", name="github_oauth_callback")
async def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    oauth_state: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if error == "access_denied":
        raise ApiError(400, "oauth_denied", "Вход отменён пользователем")
    if error is not None:
        raise ApiError(400, "oauth_error", f"GitHub вернул ошибку: {error}")
    if not code:
        raise ApiError(400, "missing_code", "Отсутствует code от GitHub")
    if not state or not oauth_state or not pysecrets.compare_digest(state, oauth_state):
        raise ApiError(400, "invalid_oauth_state", "Недействительный state-параметр")

    client_id = os.environ["GITHUB_APP_CLIENT_ID"]
    client_secret = await get_secret("GITHUB_APP_CLIENT_SECRET")

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        token_response = await http_client.post(
            GITHUB_TOKEN_URL,
            data={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise ApiError(400, "token_exchange_failed", "Не удалось получить access token от GitHub")

        auth_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        user_response = await http_client.get(GITHUB_USER_URL, headers=auth_headers)
        user_response.raise_for_status()
        profile = user_response.json()

        email = profile.get("email")
        if not email:
            emails_response = await http_client.get(GITHUB_USER_EMAILS_URL, headers=auth_headers)
            emails_response.raise_for_status()
            primary = next(
                (e for e in emails_response.json() if e.get("primary") and e.get("verified")),
                None,
            )
            if primary is None:
                raise ApiError(400, "email_unavailable", "Не удалось получить email от GitHub")
            email = primary["email"]

    github_login = profile["login"]

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email, github_login=github_login)
        db.add(user)
    else:
        user.github_login = github_login
    await db.commit()
    await db.refresh(user)

    session_jwt = await create_session_jwt(user.id)

    response = RedirectResponse(url=os.environ["FRONTEND_URL"], status_code=302)
    response.delete_cookie(OAUTH_STATE_COOKIE_NAME, path="/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_jwt,
        httponly=True,
        secure=not _is_local(),
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )
    return response


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    github_login: str | None


@router.get("/me", response_model=MeResponse)
async def me(current_user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=current_user.id, email=current_user.email, github_login=current_user.github_login)


class LogoutResponse(BaseModel):
    status: str


@router.post("/logout", response_model=LogoutResponse)
async def logout(response: Response) -> LogoutResponse:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return LogoutResponse(status="logged_out")
