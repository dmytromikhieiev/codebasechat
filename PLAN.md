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
tree-sitter (Python/Go/JS/TS, позже добавлен PHP — грамматика уже была
в `tree-sitter-languages`, новых зависимостей не потребовалось), fallback
на построчный чанкинг, gap-чанки для кода вне функций/классов. Из-за
несовместимости версий пришлось
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
- **Синхронизация установок без callback** — ✅. При реальной настройке
  выяснилось, что инструкция про GitHub App была неверной: без
  **Setup URL** GitHub вообще не редиректит браузер обратно после
  установки, и `/api/v1/github/callback` никогда не вызывается —
  README исправлен. Отдельно от этого добавлен путь, не зависящий от
  редиректа вообще: `GET /app/installations` (по App JWT) сопоставляется
  с `github_login` пользователя, без привязки к `state`/колбэку.
  - `api/services/github_app.py::list_app_installations` (с пагинацией)
  - `POST /api/v1/github/sync` — синхронизирует все репозитории установки
    или один конкретный (`repo_full_name` в теле)
  - `GET /api/v1/github/available-repos` — список репозиториев установки
    для выбора, до того как что-то синкать
  - `frontend/src/components/RepoPicker.tsx` — кнопка «Синхронизировать»
    на дашборде открывает список и даёт подключить конкретный репозиторий
- **Устойчивость к сбоям внешних API** — ✅. У Anthropic закончился баланс
  во время реальной проверки — вскрылось, что ошибка внутри уже начатого
  SSE-стрима (`stream_answer`) просто рвала соединение вместо понятного
  сообщения, а сбой retrieval/реранкинга до старта стрима отдавался
  голым 500. `api/routers/ask.py` теперь оборачивает оба места:
  до стрима — `ApiError(502, "retrieval_failed", ...)`, после старта —
  SSE-событие `error` с сообщением (фронтенд, `ChatView.tsx`, показывает
  его в чате красным). Заодно нашёлся и починен независимый баг —
  `worker/indexer.py`: полная реиндексация чистила чанки в Postgres, но
  никогда не чистила старые точки в Qdrant (`vector_store.recreate_collection`),
  и порядок записи в `_index_file`/`_remove_file` был перевёрнут
  (Qdrant раньше Postgres, из-за чего сбой между двумя записями мог
  оставить невидимую точку-сироту в Qdrant вместо самоисцеляющегося
  состояния в Postgres).
- **Конфигурируемый провайдер генерации ответа** — ✅, см.
  `.claude/tasks/answer-provider-openai.md`. `api/services/answer.py`
  теперь `AnswerProvider(ABC)` с реализациями `OpenAIAnswerProvider`/
  `AnthropicAnswerProvider`, выбор через `ANSWER_PROVIDER` (по умолчанию
  `openai`) + `ANSWER_MODEL` в `.env` — без правок кода. Публичная
  `stream_answer()` не поменяла сигнатуру, `api/routers/ask.py` не
  трогали. Эмбеддинги остаются на Voyage — их провайдер не выносился
  (смена меняет размерность вектора, нужна полная переиндексация всех
  репозиториев). Реранкинг позже вынесен аналогично, см. следующий пункт.
- **Данные Postgres/Qdrant/Redis — bind-mount вместо именованных volume'ов**
  — ✅. Обнаружилось, что после ребилда контейнеров репозитории пропадали
  из UI, хотя данные в Qdrant были на месте — `docker volume rm` во время
  перехода на Alembic стёр именно `postgres_data`, а `qdrant_data` не
  трогался, из-за чего Postgres и Qdrant разошлись (в Qdrant остались
  осиротевшие коллекции без записи в `repos`). Плюс именованные volume'ы
  в принципе привязаны к имени compose-проекта и могут "потеряться" при
  запуске из другого контекста. Теперь `docker-compose.yml` монтирует
  `./data/postgres`, `./data/qdrant`, `./data/redis` — реальные папки в
  репозитории (git-ignored), путь всегда один и тот же. Старые
  именованные volume'ы (`rag-codebase-chat_*`) не удалялись автоматически,
  но и не используются — можно почистить вручную (`docker volume rm`),
  если не нужны.
- **Конфигурируемый провайдер реранкинга** — ✅, см.
  `.claude/tasks/rerank-provider-openai.md`. По аналогии с
  `answer.py` — `api/services/reranker.py` теперь `RerankProvider(ABC)`
  с реализациями `VoyageRerankProvider` (дефолт, `rerank-2`,
  специализированный cross-encoder) и `OpenAIRerankProvider`, выбор
  через `RERANK_PROVIDER` + `RERANK_MODEL` в `.env` — без правок кода.
  У OpenAI нет отдельного rerank-эндпоинта, поэтому эта реализация
  просит чат-модель (`gpt-4o-mini` по умолчанию) оценить релевантность
  каждого сниппета через structured output (`response_format:
  json_schema`) — медленнее и дороже на вызов, чем Voyage, но не
  требует отдельного ключа. Публичная `rerank()` не поменяла сигнатуру,
  `api/routers/ask.py` не трогали.
- **Конфигурируемый провайдер эмбеддингов** — ✅, см.
  `.claude/tasks/embedding-provider-openai.md`. Та же схема —
  `api/services/embeddings.py` теперь `EmbeddingProvider(ABC)` с
  `VoyageEmbeddingProvider` (дефолт, `voyage-code-3`) и
  `OpenAIEmbeddingProvider` (`text-embedding-3-small` по умолчанию),
  выбор через `EMBEDDING_PROVIDER` + `EMBEDDING_MODEL`. Отличие от
  answer/rerank: у эмбеддингов есть жёсткий контракт по размерности —
  Qdrant-коллекция создаётся с фиксированным `QDRANT_VECTOR_SIZE = 1024`
  (`api/services/vector_store.py`), поэтому `OpenAIEmbeddingProvider`
  запрашивает `dimensions=1024` у `text-embedding-3-*` (OpenAI умеет
  урезать нативный вывод), чтобы схема коллекции не зависела от
  провайдера. Это решает совместимость схемы, но не данных: векторы
  разных провайдеров живут в разных пространствах, поэтому смена
  `EMBEDDING_PROVIDER` на уже проиндексированном репозитории требует
  полного реиндекса (кнопка «Переиндексировать») — в отличие от
  answer/rerank, это не бесплатное переключение конфига. Публичные
  `embed_documents()`/`embed_query()` не поменяли сигнатуру,
  `worker/indexer.py`/`api/services/retrieval.py` не трогали.
- **Тесты больше не бьют по dev-базе** — ✅. Обнаружилось, что
  `tests/conftest.py`'s автоюз-фикстура (чистит `users`, каскадом —
  `repos`/`chunks`/`indexing_jobs`, после каждого теста) работала против
  той же самой базы `rag`, что и запущенное приложение (`DATABASE_URL` в
  `docker-compose.yml` — один и тот же для `api`/`worker`/тестов). Каждый
  `pytest`-прогон в рамках проверки очередного фикса тихо стирал реальные
  `repos`/`chunks` пользователя. `tests/conftest.py` теперь переключает
  `DATABASE_URL` на отдельную `rag_test` (создаёт и мигрирует до `head`
  через Alembic перед первым тестом, до того как что-либо успевает
  импортировать `api.db.session`) — тесты больше не видят и не трогают
  dev-данные.
- **Индексация не только кода, но и текстовых/конфиг-файлов** — ✅, см.
  `.claude/tasks/repo-fetcher-text-files.md`. Пользователь заподозрил, что
  файлы верхнего уровня не индексируются — на деле дело было не в глубине
  пути, а в типе файла: `repo_fetcher.py` изначально (осознанно, по
  исходному ТЗ) отбрасывал любой файл без распознанного языка
  программирования, а корень репозитория обычно набит именно такими
  файлами (`README.md`, `docker-compose.yml`, `Dockerfile*` и т.д.), тогда
  как вложенные пути — в основном код. `TEXT_FILE_EXTENSIONS`/
  `TEXT_FILE_NAMES` теперь дополнительно пропускают такие файлы
  (`language="text"`, попадают в уже существовавший, но фактически
  недостижимый построчный fallback-чанкинг `chunking.py`), а
  `EXCLUDED_FILE_NAMES` явно отсекает сгенерированные лок-файлы
  (`package-lock.json`, `yarn.lock`, `go.sum` и т.п.), несмотря на то что
  их расширение формально текстовое.

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
