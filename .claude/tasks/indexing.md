# Задача: Индексация (embeddings → Qdrant → chunks)

## Контекст

Собирает воедино уже готовые `chunking.chunk_file` и `repo_fetcher.fetch_repo_files`
в фоновую RQ-задачу, вызываемую из `enqueue_indexing_job`
(`.claude/tasks/github-app-auth.md`). Архитектурные решения из `PLAN.md`:
Voyage AI (`voyage-code-3`) для эмбеддингов, Qdrant с отдельной коллекцией
на репозиторий.

`voyageai`-SDK нет в `requirements.txt` и явно не упомянут ни в одной
задаче — используем сырые HTTP-вызовы через уже имеющийся `httpx`
(как с Vault), а не добавляем новую зависимость.

## Что нужно реализовать

1. `api/services/embeddings.py` — `embed_documents(texts) -> list[vector]`,
   `embed_query(text) -> vector`, батчинг по `MAX_BATCH_SIZE` (не один
   запрос на чанк), ключ через `secrets.get_secret("VOYAGE_API_KEY")`
2. `api/services/vector_store.py` — `AsyncQdrantClient`, коллекция
   `repo_{repo_id}` на репозиторий (создаётся при первом обращении),
   `upsert_points` с payload `file_path`/`start_line`/`end_line`/`function_name`
3. `api/db/models.py` — добавить `IndexingJob`, `Chunk` (зеркалят оставшиеся
   таблицы `migrations/0001_init.sql`)
4. `worker/indexer.py` — `run_indexing_job(repo_id: str)`, синхронная
   точка входа для RQ (оборачивает `asyncio.run` — сам RQ job sync):
   - создаёт `IndexingJob(status="running")`, `repos.status = "indexing"`
   - `fetch_repo_files` → на каждый файл `chunking.chunk_file` → батч
     эмбеддингов → `vector_store.upsert_points` → запись строк `chunks`
     (старые чанки репозитория удаляются перед полной переиндексацией)
   - после каждого файла обновляет `indexing_jobs.files_done`/`progress`
   - по завершении: `repos.status = "ready"`, `repos.indexed_at`,
     `indexing_jobs.status = "succeeded"`, `progress = 1.0`
   - при исключении: `repos.status = "failed"`, `indexing_jobs.status = "failed"`,
     `error = str(exc)`, исключение пробрасывается дальше (чтобы RQ тоже
     видел job как failed)

## Критерии готовности

- [ ] Мок Voyage API через `respx` — реальные вызовы запрещены
- [ ] Батчинг: N чанков не порождают N HTTP-запросов к Voyage
- [ ] `indexing_jobs.progress`/`files_done` растут по ходу обработки файлов,
      не одним скачком в конце
- [ ] Успешный прогон: `repos.status == "ready"`, `chunks` содержит строки
      для всех чанков всех файлов, `content_hash` заполнен
- [ ] Ошибка на любом шаге → `repos.status == "failed"`,
      `indexing_jobs.error` заполнен, задача не падает молча
- [ ] Тесты `vector_store.py` — против реального Qdrant из `docker-compose`
      (как с Postgres — это локальная инфраструктура, не внешний API,
      мокать не нужно), с очисткой созданных коллекций после теста

## Известные ограничения (сознательно не решаем сейчас)

- Каждый прогон — полная переиндексация (удаляем и пересоздаём все чанки
  репозитория); инкрементальная реиндексация по `content_hash` — это
  Фаза 5 (webhooks), `content_hash` только заполняется, но не используется
  для диффа
- Embedding batching происходит на уровне одного файла, не всего
  репозитория — проще для прогресса, чуть менее эффективно по числу
  HTTP-запросов к Voyage

## Не делать

- Не реализовывать retrieval/reranking/ответ — это следующая задача
- Не реализовывать webhook-триггер реиндексации — Фаза 5
- Не добавлять `voyageai` SDK — используем `httpx`
