# PLAN.md — план разработки

Этот файл — точка входа для Claude Code. Прочитай его целиком перед тем,
как браться за любую задачу в этом репозитории. Здесь — идея проекта,
принятые архитектурные решения и пошаговый план реализации по фазам.

Конвенции кода, структура папок и правила тестирования — в `CLAUDE.md`,
он читается вместе с этим файлом.

## Идея проекта

RAG-приложение "chat with your codebase". Пользователь подключает свой
GitHub-репозиторий (публичный или приватный), система индексирует код,
и дальше можно задавать вопросы на естественном языке — "где обрабатывается
retry-логика в HTTP-клиенте?", "как устроена аутентификация?" — и получать
ответ со ссылками на конкретные файлы и строки, а не общие рассуждения.

Ключевая ценность — ответ всегда подкреплён реальным кодом из репозитория,
а не тем, что модель "помнит" из обучения.

## Принятые архитектурные решения (важно не отступать от них без причины)

- **Аутентификация — GitHub App, не SSH-ключ.** Пользователь устанавливает
  приложение через стандартный GitHub-флоу, мы получаем `installation_id`
  и обмениваем его на короткоживущий токен. Никаких SSH-ключей пользователя
  нигде в системе.
- **Доступ к коду — через GitHub API, без git clone.** Tarball API для
  полной индексации, Git Trees/Compare API для инкрементальных апдейтов
  по webhook на push. Никакого клонирования, никакого файлового диска
  с чужим кодом.
- **Чанкинг — по AST, не по количеству символов.** tree-sitter, границы
  чанков совпадают с границами функций/классов. Реализовано и
  протестировано — см. `api/services/chunking.py`.
- **Retrieval — гибридный.** BM25 (полнотекстовый поиск по Postgres,
  индекс уже создан в миграции) + векторный поиск (Qdrant) → cross-encoder
  реранкинг top-20 в top-5. Не полагаться только на векторный поиск.
- **Эмбеддинги — Voyage AI (voyage-code-3).** Managed API, не нужен
  собственный GPU-хостинг для MVP.
- **Ответ — стримингом через SSE**, источники (`sources`) отдаются
  отдельным event до токенов ответа, чтобы фронт мог показать их сразу.
- **Логин пользователя — GitHub OAuth (user-to-server), сессия — stateless
  JWT в подписанной cookie.** Отдельно от App JWT для installation-токенов:
  у GitHub App свои `client_id`/`client_secret` для идентификации
  пользователя. Сессия не хранится на сервере (без новой таблицы в БД) —
  подписанный токен с `user_id` и `exp`, проверяется по `SESSION_SECRET_KEY`.
- **Привязка installation_id к пользователю — подписанный `state`.**
  Endpoint инициации установки генерирует `state` (JWT/HMAC с `user_id`),
  GitHub возвращает его в `/callback` без изменений — callback проверяет
  подпись, без обращения к БД/Redis за pending-состоянием.

## Текущее состояние репозитория (уже реализовано)

- `CLAUDE.md` — конвенции проекта
- `alembic/versions/0001_init.py` — полная схема БД (users, repos,
  indexing_jobs, chunks, queries, query_feedback); применяется через
  сервис `migrate` в `docker-compose.yml` (изначально были raw SQL-файлы
  в `migrations/`, позже перевели на Alembic)
- `docker-compose.yml`, `Dockerfile.api`, `Dockerfile.worker`,
  `requirements.txt` — вся инфраструктура поднимается через
  `docker compose up --build`
- `api/main.py` — FastAPI-каркас с `/health` и глобальным обработчиком
  ошибок (`api/errors.py`, формат `{"error", "message"}` из `CLAUDE.md`)
- `.claude/tasks/user-login.md` — реализовано: GitHub OAuth логин,
  stateless JWT-сессия (`api/services/auth.py`, `api/routers/auth.py`),
  `GET /api/v1/github/install`
- `.claude/tasks/github-app-auth.md` — реализовано: App JWT/installation
  token (`api/services/github_app.py`), `GET /api/v1/github/callback`,
  upsert `repos`, постановка в очередь (`api/services/queue.py`)
- `api/db/` — `User`, `Repo`, `IndexingJob`, `Chunk`, `Query`, `QueryFeedback`
  (зеркалят `alembic/versions/0001_init.py`, включая индексы и constraint'ы —
  важно для безопасного `alembic revision --autogenerate` в будущем)
- `api/services/secrets.py` — обёртка над Vault с fallback на `.env`
  в `APP_ENV=local`
- 26 тестов, `docker compose exec api pytest` зелёный

**Важно**: `api/services/chunking.py` (AST-чанкер), вопреки более ранней
версии этого файла, **не существовал** — ниже он в списке задач наравне
с остальными, а не как готовая зависимость.

Всё остальное ниже — то, что нужно реализовать.

## План реализации MVP

Фазы убраны — весь список ниже реализуется как единый MVP. Порядок
внутри списка важен и отражает реальные зависимости (например,
retrieval не заработает без данных, которые появляются только после
индексации), но задачи не обязаны сдаваться отдельными этапами —
имеет смысл заводить файлы в `.claude/tasks/` по мере необходимости,
с тем же уровнем детализации, что и `github-app-auth.md`.

**Логин и подключение репозитория (auth)** — ✅ готово, см.
`.claude/tasks/user-login.md` и `.claude/tasks/github-app-auth.md`.

**AST-чанкинг** — ✅ готово, см. `.claude/tasks/chunking.md`.
`api/services/chunking.py`: `chunk_file`/`detect_language`/`CHUNK_NODE_TYPES`,
tree-sitter (Python/Go/JS/TS), fallback на построчный чанкинг, gap-чанки
для кода вне функций/классов. Из-за несовместимости версий пришлось
понизить `tree-sitter` до `0.21.3` в `requirements.txt` (`tree-sitter-languages`
1.10.2 не работает с 0.22+).

**Получение содержимого репозитория без клонирования** — ✅ готово, см.
`.claude/tasks/repo-fetcher.md`. `api/services/repo_fetcher.py`:
`fetch_repo_files` — tarball в память, фильтрация по чёрному списку
директорий и по `chunking.detect_language`, устойчиво к бинарникам/не-UTF-8.
Известное ограничение: весь tarball грузится в память целиком, без
стриминга — ок для MVP.

**Индексация** — ✅ готово, см. `.claude/tasks/indexing.md`.
`api/services/embeddings.py` (Voyage через `httpx`, батчинг),
`api/services/vector_store.py` (Qdrant, коллекция на репо),
`worker/indexer.py` (RQ job: fetch → chunk → embed → upsert → `chunks`,
инкрементальный `progress`/`files_done`, `repos.status` ready/failed).
Полная переиндексация на каждый прогон — `content_hash` пишется, но для
диффа пока не используется (это Фаза 5/webhooks).

**Retrieval и генерация ответа** — ✅ готово, см.
`.claude/tasks/retrieval-and-answer.md`. `api/services/retrieval.py`
(гибридный поиск: BM25 по Postgres + Qdrant, слияние через Reciprocal
Rank Fusion по `chunks.embedding_id`), `api/services/reranker.py`
(Voyage `rerank-2`, не `sentence-transformers` — не тянем `torch`),
`api/services/answer.py` (`anthropic.AsyncAnthropic`, стриминг),
`api/routers/ask.py` (`POST /api/v1/repos/{repo_id}/ask`, SSE
`sources`→`token`×N→`done`, запись в `queries`).

**Webhooks и инкрементальные апдейты** — ✅ готово, см.
`.claude/tasks/webhooks.md`. `api/routers/webhooks.py` (проверка
`X-Hub-Signature-256`, реагирует только на `push` в default_branch),
`worker/indexer.py::run_incremental_indexing_job` (`compare` API,
удаление чанков/точек для `removed`/`renamed`, переиндексация
изменённых). Попутно выяснилось, что полная индексация никогда не
писала `last_indexed_sha` — теперь резолвит SHA через
`github_app.resolve_ref_to_sha` и пишет его по завершении.

**Полировка** — ✅ готово, см. `.claude/tasks/mvp-polish.md`.
`api/services/rate_limit.py` (фиксированное окно через Redis, per-repo),
`api/routers/feedback.py` (`POST /api/v1/queries/{query_id}/feedback`,
`QueryFeedback` в `api/db/models.py`). Юнит-тесты писались по ходу
реализации каждого блока, а не одним отдельным проходом в конце.

## После MVP

Изначально фронтенд был опциональным пунктом и не делался. Позже его
явно запросили, плюс по ходу настройки GitHub App всплыла пара
пробелов в API — оба закрыты отдельными задачами:

- **Ручной триггер реиндексации** — ✅, см. `.claude/tasks/manual-reindex.md`.
  `POST /api/v1/repos/{repo_id}/reindex` — та же логика, что у webhook
  (полный реиндекс либо инкрементальный по текущему HEAD), но по запросу
  пользователя. Нужен, если Webhook URL в GitHub App не настроен
  (не требует `ngrok`/`smee.io` для локальной разработки).
- **Фронтенд** — ✅, см. `.claude/tasks/frontend.md`. React + Vite +
  TypeScript + Tailwind, SPA в `frontend/`, отдельный контейнер в
  `docker-compose.yml`. Заодно добавлены недостающие для UI
  backend-эндпоинты: `GET /api/v1/auth/me`, `POST /api/v1/auth/logout`,
  `GET /api/v1/repos`, `GET /api/v1/repos/{repo_id}`, и CORS-middleware.
  `/ask` — SSE через `POST`, что `EventSource` не поддерживает, поэтому
  парсинг потока сделан вручную (`fetch` + `ReadableStream`) — токены
  дорисовываются в UI по мере прихода, без ожидания конца генерации.

## Критерии готовности MVP

- [x] Пользователь может подключить приватный репозиторий через GitHub App
- [x] Репозиторий индексируется в фоне, статус виден через API
      (`repos.status`, `indexing_jobs.progress`)
- [x] Можно задать вопрос и получить стримингом ответ со ссылками на
      файл + строку (`POST /api/v1/repos/{repo_id}/ask`)
- [x] При пуше в репозиторий индекс обновляется инкрементально, без
      полной переиндексации (`POST /api/v1/webhooks/github`, либо вручную
      через `POST /api/v1/repos/{repo_id}/reindex`)
- [x] Все внешние вызовы (GitHub, Voyage, Anthropic) покрыты тестами
      с моками; Postgres/Qdrant/Redis — против реальных инстансов из
      `docker-compose`, не мокаются (см. `CLAUDE.md` — это внешние API,
      локальная инфраструктура правилу не подпадает)
- [x] Есть веб-интерфейс — не только API

MVP и всё, что запросили сверху, реализовано целиком, 93 теста проходят
(`docker compose exec api pytest`).

## Как двигаться по этому плану

Начинай с блока «Логин и подключение репозитория (auth)». Для части
про installation token задача уже описана в
`.claude/tasks/github-app-auth.md`; для части про логин (`api/routers/auth.py`,
`api/services/auth.py`, endpoint инициации установки с подписанным `state`)
нужно завести отдельный файл в `.claude/tasks/` с тем же уровнем
детализации, прежде чем начинать реализацию — задание должно явно описать
формат JWT-сессии и `state`, обработку ошибок OAuth (`error` в query),
и что делать при повторном логине существующего `github_login`.

Перед тем как браться за следующий блок из списка выше, заводи новый файл
в `.claude/tasks/` с тем же уровнем детализации (критерии готовности, что
не трогать), используя соответствующий раздел плана как черновик. Порядок
в списке — это порядок зависимостей, а не формальные этапы: retrieval не
заработает без данных, которые появляются только после индексации,
поэтому не перескакивай вперёд по факту, даже если явных «фаз» больше нет.
