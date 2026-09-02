# Задача: GitHub App OAuth вместо SSH-ключа

## Контекст
RAG-приложение индексирует приватные репозитории. Вместо SSH deploy key
используем GitHub App — токен короткоживущий, скоупится через installation,
не требует хранения приватного ключа пользователя.

Зависит от `.claude/tasks/user-login.md`: оттуда приходят
`GET /api/v1/github/install` (генерирует `state` и редиректит пользователя
на установку app) и `verify_install_state_jwt` в `api/services/auth.py`
(проверяет этот `state` и возвращает `user_id`). Эта задача логин и install-
инициацию не трогает — только принимает результат установки.

## Что нужно реализовать
1. `GET /api/v1/github/callback` — принимает `installation_id`,
   `setup_action` и `state` от GitHub после установки app
   (`api/routers/github.py`)
2. Проверка владения: `verify_install_state_jwt(state)` → `user_id`.
   Если `state` отсутствует/невалиден/просрочен — 403 (защита от подмены;
   без state нет способа понять, кто инициировал установку)
3. `get_installation_token(installation_id)` в `api/services/github_app.py` —
   генерирует App JWT (RS256, iss=GITHUB_APP_ID, exp <= 10 минут) и обменивает
   на installation access token через
   POST /app/installations/{id}/access_tokens
4. Приватный ключ GitHub App читается из Vault-клиента
   (`api/services/secrets.py`), не из .env напрямую
5. После успешной проверки — создать/обновить запись в `repos` (owner_id =
   `user_id` из `state`, см. `migrations/0001_init.sql`), поставить job на
   индексацию в очередь (`enqueue_indexing_job`), затем `302` редирект на
   `FRONTEND_URL`

## Критерии готовности
- [ ] Юнит-тесты на `generate_app_jwt` (проверить exp/iat)
- [ ] Мок httpx-запроса к GitHub API в тестах (respx), не реальный вызов
- [ ] Обработка ошибки: `state` отсутствует/невалиден/просрочен → 403
- [ ] Обработка `setup_action == "request"` (юзер без прав на репо):
      `installation_id` в этом случае от GitHub не приходит или
      недействителен — в `repos` ничего не пишем, `enqueue_indexing_job`
      не вызываем, `302` редирект на `FRONTEND_URL` с пометкой об ожидании
      одобрения (например, query-параметром)
- [ ] Токен нигде не логируется целиком (только последние 4 символа)
- [ ] Повторный callback с уже использованным/просроченным `state`
      не приводит к 500 (ожидаемая структурированная ошибка)

## Не делать
- Не трогать retrieval-пайплайн (`api/services/retrieval.py`)
- Не добавлять SSH-related код — эта задача его заменяет
- Не реализовывать логин, сессию и `GET /api/v1/github/install` — это
  `user-login.md`, здесь только потребление `verify_install_state_jwt`
