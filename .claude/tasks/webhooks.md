# Задача: Webhooks и инкрементальная реиндексация

## Контекст

`PLAN.md`: push в репозиторий должен обновлять индекс инкрементально —
только изменённые файлы, через `compare` API между `repos.last_indexed_sha`
и новым SHA, без полной переиндексации.

Побочная зависимость, всплывшая только сейчас: полная индексация
(`.claude/tasks/indexing.md`) никогда не заполняла `repos.last_indexed_sha`
(эта колонка не была нужна до этой задачи) — без неё не от чего строить
`compare`. Решение: не трогать `repo_fetcher.py` (парсинг SHA из имени
корневой директории tar-архива неоднозначен — owner/repo сами могут
содержать дефисы), а добавить в `github_app.py` отдельный вызов
`GET /repos/{full_name}/commits/{ref}` и резолвить SHA явно. Полная
индексация (`worker/indexer.py::_index_repo`) теперь тоже пишет
`last_indexed_sha` по итогам прогона.

## Что нужно реализовать

1. `api/services/github_app.py`:
   - `resolve_ref_to_sha(installation_id, repo_full_name, ref) -> str`
   - `compare_commits(installation_id, repo_full_name, base, head) -> list[dict]` —
     `GET /repos/{full_name}/compare/{base}...{head}`, возвращает `files`
     (каждый: `filename`, `status`, опционально `previous_filename`)
2. `api/services/vector_store.py` — `delete_points_by_file_path(repo_id, file_path)`
   (фильтр по payload `file_path`)
3. `worker/indexer.py`:
   - `_index_repo` (полная индексация) — резолвит SHA ветки через
     `resolve_ref_to_sha` и пишет в `repos.last_indexed_sha` по завершении
   - `run_incremental_indexing_job(repo_id, base_sha, head_sha)` — новая
     точка входа для RQ: `compare_commits` → для `removed`/`renamed` старого
     пути удаляет чанки (Postgres + Qdrant), для остальных статусов
     перечитывает содержимое файла (через `repo_fetcher.fetch_repo_files`
     на `head_sha`) и переиндексирует; по завершении обновляет
     `repos.last_indexed_sha = head_sha`. Прогресс/ошибки — как в полной
     индексации (`indexing_jobs`)
4. `api/services/queue.py` — `enqueue_incremental_indexing_job(repo_id, base_sha, head_sha)`
5. `api/routers/webhooks.py` — `POST /api/v1/webhooks/github`:
   - проверка `X-Hub-Signature-256` по `GITHUB_APP_WEBHOOK_SECRET`
     (HMAC-SHA256 от сырого тела запроса, `hmac.compare_digest`) — 401 при
     несовпадении/отсутствии
   - реагирует только на `X-GitHub-Event: push`, остальные (`ping`,
     `installation`, ...) — 200 без действий (подпись всё равно проверяется)
   - находит `Repo` по `repository.full_name` + `installation.id`; не
     найден → 200 без действий (не 404 — GitHub не должен ретраить)
   - игнорирует пуши не в `default_branch` и пуши с удалением ветки
     (`after` — 40 нулей)
   - если `repos.last_indexed_sha` пуст — ставит полную индексацию
     (`enqueue_indexing_job`), иначе инкрементальную

## Критерии готовности

- [ ] Валидная подпись — обязательна; неверная/отсутствующая → 401,
      реальный воркер задачу не получает
- [ ] `ping`-событие отвечает 200 (иначе GitHub считает webhook неисправным)
- [ ] Push не в default_branch — игнорируется, ничего не ставится в очередь
- [ ] Push с пустым/незнакомым `repository.full_name` — 200, не 500
- [ ] Инкрементальная реиндексация: `removed` и `renamed` (старый путь)
      удаляют чанки из Postgres И точки из Qdrant; изменённые файлы
      переиндексируются; `last_indexed_sha` обновляется на новый SHA
- [ ] Полная индексация теперь тоже проставляет `last_indexed_sha`
      (регрессионный тест на `worker/test_indexer.py`)
- [ ] Мок httpx/respx для GitHub compare/commits API — реальные вызовы
      запрещены

## Известные ограничения (сознательно не решаем сейчас)

- Инкрементальная реиндексация всё ещё скачивает полный tarball на
  `head_sha` (через `fetch_repo_files`), чтобы получить контент
  изменившихся файлов — нет точечного скачивания через Contents API
- Нет обработки `installation_repositories`/`installation` webhook-событий
  (добавление/удаление репозиториев или деинсталляция app) — только `push`

## Не делать

- Не трогать `repo_fetcher.py` — извлечение SHA сделано отдельным вызовом
  GitHub API в `github_app.py`, не парсингом имени архива
- Не реализовывать rate limiting — Фаза 6
