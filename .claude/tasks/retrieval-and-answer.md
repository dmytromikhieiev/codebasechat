# Задача: Retrieval, реранкинг и ответ (SSE)

## Контекст

`PLAN.md`: retrieval — гибридный (BM25 по Postgres + векторный поиск в
Qdrant), затем cross-encoder реранкинг top-20 → top-5, ответ — Claude API
потоково через SSE, `sources` отдаются отдельным event до токенов.

Реранкинг решено делать через **Voyage rerank API** (`rerank-2`), а не
через `sentence-transformers`/локальную cross-encoder модель — та требует
`torch` и сильно раздувает образ, а Voyage-ключ уже сконфигурирован для
эмбеддингов. Не добавляет новых зависимостей.

## Что нужно реализовать

1. `api/db/models.py` — добавить `Query` (таблица `queries`)
2. `api/services/vector_store.py` — добавить `search(repo_id, query_vector, limit)`
3. `api/services/retrieval.py`:
   - `RetrievedChunk` — dataclass (`id`, `file_path`, `start_line`,
     `end_line`, `function_name`, `language`, `content`, `score: float | None`)
   - `hybrid_search(db, repo_id, query) -> list[RetrievedChunk]` — top-20
     через Reciprocal Rank Fusion (`k=60`) между BM25-результатами
     (`to_tsvector`/`plainto_tsquery`/`ts_rank` по `chunks.content`,
     индекс `idx_chunks_content_fts` уже есть) и векторным поиском
     (`embed_query` + `vector_store.search`). Общий ключ между двумя
     источниками — `chunks.embedding_id` (он же id точки в Qdrant), не
     `chunks.id`
4. `api/services/reranker.py` — `rerank(query, chunks, top_k=5)` через
   `POST /v1/rerank` (модель `rerank-2`), возвращает чанки с проставленным
   `score` (`relevance_score` от Voyage)
5. `api/services/answer.py` — `stream_answer(question, chunks) -> AsyncIterator[str]`,
   `anthropic.AsyncAnthropic`, промпт из кодовых сниппетов top-5, системный
   промпт запрещает отвечать вне предоставленных сниппетов
6. `api/routers/ask.py` — `POST /api/v1/repos/{repo_id}/ask`:
   - 404 если репозиторий не найден или не принадлежит текущему
     пользователю (`get_current_user`)
   - 409 если `repo.status != "ready"`
   - `StreamingResponse` (`text/event-stream`): `event: sources` (список
     чанков без содержимого — только file_path/start_line/end_line/
     function_name) → `event: token` на каждый кусок текста → `event: done`
     с `query_id` после того как строка в `queries` записана
     (`question`, `answer`, `retrieved_chunk_ids`, `latency_ms`)

## Критерии готовности

- [ ] Мок Voyage (embeddings + rerank) и Anthropic API через `respx`/mock —
      реальные вызовы запрещены
- [ ] `hybrid_search` тестируется против реального Postgres+Qdrant
      (как остальная инфраструктура) с заранее вставленными чанками —
      проверить, что чанк, найденный только BM25 ИЛИ только вектором,
      всё равно попадает в объединённый результат
- [ ] 404 на чужой/несуществующий repo_id, 409 на непроиндексированный
- [ ] После полного стриминга строка в `queries` содержит собранный
      `answer` целиком и `retrieved_chunk_ids` с оценками
- [ ] Порядок SSE-событий: `sources` строго до первого `token`, `done`
      строго после записи в БД

## Известные ограничения (сознательно не решаем сейчас)

- Rate limiting на `/ask` — отдельная задача (Фаза 6)
- Нет defensive-обрезки промпта по токенам, если топ-5 чанков окажутся
  аномально большими — на MVP-масштабе не критично

## Не делать

- Не реализовывать webhooks/инкрементальную реиндексацию — Фаза 5
- Не добавлять `sentence-transformers`/`torch` — реранкинг через Voyage
- Не трогать `worker/indexer.py`, `chunking.py`, `repo_fetcher.py`
