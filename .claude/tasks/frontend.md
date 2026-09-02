# Задача: Фронтенд — чат-интерфейс

## Контекст

`PLAN.md` отмечал фронтенд опциональным пунктом Фазы 6 и не делал его.
Теперь нужен реальный UI: логин, список/подключение репозиториев, чат
с потоковым (token-by-token) отображением ответа, а не ожидание полного
текста.

Выбор: React + Vite + TypeScript + Tailwind — SPA без SSR (приватный
инструмент, SEO не нужен), Vite для простой сборки, Tailwind вместо
ручного CSS. Отдельная директория `frontend/`, отдельный контейнер
в `docker-compose.yml`.

Важный технический момент: `POST /api/v1/repos/{repo_id}/ask` — это SSE,
но через `POST` с JSON-телом, а браузерный `EventSource` умеет только
`GET`. Стриминг разбирается вручную: `fetch()` + `ReadableStream`-ридер,
парсинг блоков `event: ...\ndata: ...\n\n` по мере прихода чанков —
токены должны отрисовываться в UI сразу, не после закрытия потока.

Cookie-сессия (`SameSite=Lax`) между `localhost:3000` (фронт) и
`localhost:8000` (api) работает без изменений: SameSite сравнивает
только домен, порт не учитывается — `localhost:3000` и `localhost:8000`
считаются одним "сайтом". Единственное, чего не хватает — CORS
(порт всё-таки часть origin для CORS) и `credentials: "include"` на
фронтовых fetch-запросах.

## Что нужно реализовать

### Backend (недостающие эндпоинты для UI)

1. `api/main.py` — `CORSMiddleware`, `allow_origins=[FRONTEND_URL]`,
   `allow_credentials=True`
2. `api/routers/auth.py` — `POST /api/v1/auth/logout` (удаляет cookie
   сессии), `GET /api/v1/auth/me` (текущий пользователь: `id`, `email`,
   `github_login`; 401 без сессии — фронту нужно чем-то проверять,
   залогинен ли пользователь, при загрузке страницы)
3. `api/routers/repos.py`:
   - `GET /api/v1/repos` — репозитории текущего пользователя
   - `GET /api/v1/repos/{repo_id}` — репозиторий + прогресс последней
     `indexing_jobs` записи (для поллинга во время индексации)

### Frontend (`frontend/`)

- `LoginPage` — кнопка "Войти через GitHub" → `GET /api/v1/auth/login`
  (обычная навигация, не fetch)
- `Dashboard` — после логина: `GET /api/v1/auth/me` + `GET /api/v1/repos`;
  кнопка "Подключить репозиторий" → `GET /api/v1/github/install`;
  список репозиториев со статусом (индикатор прогресса, пока
  `status == "indexing"` — поллинг `GET /api/v1/repos/{id}` раз в
  несколько секунд), кнопка "Переиндексировать" (`POST .../reindex`)
- `ChatView` — для выбранного `status == "ready"` репозитория:
  - инпут вопроса → `POST /api/v1/repos/{id}/ask`, ручной SSE-парсинг
  - `sources`-событие → показать список файлов/строк над ответом
  - `token`-события → дописывать в текущий пузырь ответа по мере прихода
    (это причина, почему нельзя использовать `await response.json()`)
  - `done`-событие → `query_id`, показать 👍/👎 →
    `POST /api/v1/queries/{query_id}/feedback`

### Инфраструктура

- `Dockerfile.frontend`, сервис `frontend` в `docker-compose.yml`
  (`vite --host`, порт 3000, `env_file: .env`, `VITE_API_BASE_URL`)

## Критерии готовности

- [ ] Тесты на новые backend-эндпоинты (`/me`, `/logout`, `GET /repos`,
      `GET /repos/{id}`) — 401 без сессии, чужие репозитории не видны
- [ ] CORS: fetch с `http://localhost:3000` к API проходит с cookie
      (`credentials: "include"`)
- [ ] В чате токены реально появляются по одному, а не всей строкой разом
      после завершения генерации — проверить вживую в браузере
- [ ] `npm run build` во фронтенде проходит без ошибок TypeScript

## Не делать

- Не делать SSR/Next.js — SPA достаточно
- Не заводить отдельную аутентификацию фронта — вся сессия на cookie
  от API, фронт только читает `/api/v1/auth/me`
- Не переносить сам `/ask`-эндпоинт на `EventSource`/`GET` — контракт
  API не трогаем, тело вопроса больше, чем помещается в query-string
  для произвольных вопросов
