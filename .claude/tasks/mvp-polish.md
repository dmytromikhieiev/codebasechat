# Задача: Полировка MVP — rate limiting и feedback

## Контекст

Последний пункт `PLAN.md`. Юнит-тесты на предыдущие блоки уже написаны
по ходу их реализации — здесь только то, что оставалось: rate limiting
на `/ask` и эндпоинт фидбэка. Простой фронтенд — опционально, не делаем
(нет отдельного задания и явного запроса).

## Что нужно реализовать

1. `api/services/rate_limit.py` — `enforce_ask_rate_limit(repo_id)`:
   фиксированное окно через Redis (`INCR` + `EXPIRE` на первый инкремент),
   ключ на репозиторий (`ratelimit:ask:{repo_id}`), лимит по умолчанию
   `RATE_LIMIT_MAX_REQUESTS=20` за `RATE_LIMIT_WINDOW_SECONDS=60`.
   Превышение → `ApiError(429, "rate_limited", ...)`
2. `api/routers/ask.py` — вызвать `enforce_ask_rate_limit` сразу после
   проверки владения/статуса репозитория, до похода в Voyage/Anthropic
3. `api/db/models.py` — добавить `QueryFeedback` (таблица `query_feedback`)
4. `api/routers/feedback.py` — `POST /api/v1/queries/{query_id}/feedback`:
   тело `{"rating": -1 | 1, "comment": str | None}` (`Literal[-1, 1]` в
   Pydantic-схеме — невалидный rating отсекается на уровне валидации,
   422 автоматически), 404 если query не существует или принадлежит
   другому пользователю

## Критерии готовности

- [ ] `enforce_ask_rate_limit` — N-й запрос сверх лимита → 429, лимит
      per-repo (у другого repo_id свой счётчик)
- [ ] `/ask` при превышении лимита не делает вызовов к Voyage/Anthropic
- [ ] `POST /queries/{id}/feedback` без сессии → 401, чужой query_id → 404,
      `rating` вне `{-1, 1}` → 422
- [ ] Тесты `rate_limit.py` — против реального Redis (как Postgres/Qdrant,
      локальная инфраструктура, не мокается)

## Не делать

- Не реализовывать фронтенд
- Не менять лимиты/окно без явного запроса — выбраны как разумный дефолт
  для MVP, не подобраны под нагрузочный профиль
