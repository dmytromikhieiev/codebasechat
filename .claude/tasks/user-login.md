# Задача: Логин пользователя через GitHub OAuth + сессия

## Контекст

Сейчас в проекте нет способа определить, кто делает запрос — `users` в БД
есть, но никто в неё не пишет и никакая сессия не проверяется. Это блокирует
`.claude/tasks/github-app-auth.md`: там на callback нужно знать "текущего
пользователя", чтобы проверить, что `installation_id` принадлежит ему.

Эта задача закрывает именно вход и сессию. Обмен `installation_id` на
installation-токен и сама логика `/api/v1/github/callback` — не здесь,
это `github-app-auth.md`.

Архитектурное решение (см. `PLAN.md`): логин — GitHub OAuth (user-to-server,
через тот же GitHub App, но отдельные `client_id`/`client_secret`), сессия —
stateless JWT в подписанной cookie, без таблицы сессий в БД.

## Что нужно реализовать

1. `api/services/auth.py`:
   - `create_session_jwt(user_id: UUID) -> str` / `verify_session_jwt(token: str) -> UUID` —
     сессионный токен. Claim `typ: "session"`, `exp` — рекомендуется 14 дней.
   - `create_install_state_jwt(user_id: UUID) -> str` / `verify_install_state_jwt(token: str) -> UUID` —
     токен для `state`-параметра установки GitHub App. Claim `typ: "install_state"`,
     `exp` ≤ 10 минут. **Обязательно разные `typ`** — сессионный токен не должен
     проходить проверку как install-state и наоборот (иначе один JWT можно
     переиспользовать не по назначению).
   - Оба подписаны `SESSION_SECRET_KEY` (новая переменная окружения).
   - `get_current_user` — FastAPI-dependency: читает cookie сессии, вызывает
     `verify_session_jwt`, подгружает `User` из БД (не доверять только payload —
     пользователь мог быть удалён). Отсутствие/невалидность cookie → 401.

2. `api/routers/auth.py`:
   - `GET /api/v1/auth/login` — редирект на
     `https://github.com/login/oauth/authorize` с `client_id`, `redirect_uri`,
     `scope=read:user user:email`, и CSRF-`state` (случайный nonce, кладётся
     в короткоживущую отдельную cookie `oauth_state`, не путать с install-state
     из пункта 1).
   - `GET /api/v1/auth/callback`:
     - сверяет `state` из query с `oauth_state` из cookie (защита от CSRF),
       cookie после использования удаляется
     - обрабатывает `error=access_denied` (пользователь отменил авторизацию)
     - обменивает `code` на user access token: `POST
       https://github.com/login/oauth/access_token`
     - получает профиль: `GET https://api.github.com/user`; если `email`
       пустой (приватный) — дополнительно `GET https://api.github.com/user/emails`,
       берёт primary+verified адрес (это может быть `...@users.noreply.github.com`,
       это нормально)
     - upsert в `users` (уникальность по `email`; повторный логин
       существующего `github_login` не создаёт дубликат)
     - выставляет cookie сессии (`create_session_jwt`), `302` редирект на
       `FRONTEND_URL`

3. `api/routers/github.py` — **добавить** (не трогая остальной роутер,
   которого пока не существует и который заведёт `github-app-auth.md`):
   - `GET /api/v1/github/install` — требует `get_current_user`, генерирует
     `create_install_state_jwt(user.id)`, редиректит на
     `https://github.com/apps/{GITHUB_APP_SLUG}/installations/new?state=...`

4. Новые переменные окружения (добавить в `.env.example`):
   `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_SLUG`,
   `SESSION_SECRET_KEY`, `FRONTEND_URL`

## Критерии готовности

- [ ] Юнит-тесты `create_session_jwt`/`verify_session_jwt`: payload, `exp`,
      отклонение просроченного и подделанного токена
- [ ] Юнит-тесты `create_install_state_jwt`/`verify_install_state_jwt`,
      включая проверку, что сессионный токен не проходит как install-state
      и наоборот (проверка `typ`)
- [ ] Мок всех httpx-запросов к GitHub (`/login/oauth/access_token`,
      `/user`, `/user/emails`) через `respx` — реальные вызовы в тестах
      запрещены (см. `CLAUDE.md`)
- [ ] Обработка `error=access_denied` на `/callback` — не 500, понятная
      структурированная ошибка
- [ ] Обработка невалидного/просроченного/отсутствующего `state` (CSRF) → 400
- [ ] Кейс отсутствия публичного email — фоллбэк на `/user/emails`
- [ ] Повторный логин существующего пользователя не создаёт дубликат в `users`
- [ ] Cookie сессии: `HttpOnly`, `Secure` (вне `APP_ENV=local`), `SameSite=Lax`
- [ ] User access token GitHub нигде не логируется целиком и не сохраняется
      в БД — используется только в рамках обработки `/callback`
- [ ] `GET /api/v1/github/install` без валидной сессии → 401

## Не делать

- Не реализовывать обмен `installation_id` на installation-токен и логику
  самого `GET /api/v1/github/callback` (кроме нового `GET .../install`) —
  это `github-app-auth.md`. Тот таск должен использовать
  `verify_install_state_jwt` из `api/services/auth.py` для проверки
  владения installation.
- Не заводить таблицу сессий и не менять схему БД — сессия stateless
- Не добавлять новые внешние зависимости — `pyjwt` уже есть в `requirements.txt`
- Не трогать retrieval-пайплайн и остальные части, не связанные с логином
