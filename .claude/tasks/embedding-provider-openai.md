# Задача: Конфигурируемый провайдер эмбеддингов (Voyage + OpenAI)

## Контекст

`api/services/embeddings.py` жёстко использовал Voyage (`voyage-code-3`).
По аналогии с генерацией ответа и реранкингом (см.
[[answer-provider-openai]], [[rerank-provider-openai]]) нужен полиморфный
выбор провайдера эмбеддингов через конфиг — Voyage остаётся дефолтом
(модель заточена под код), OpenAI добавляется как alternative.

Важное отличие от answer/rerank: эмбеддинги хранятся в Qdrant, у
коллекции фиксированная размерность вектора (`QDRANT_VECTOR_SIZE = 1024`
в `api/services/vector_store.py`). У OpenAI `text-embedding-3-*` есть
параметр `dimensions`, которым можно урезать нативный вывод модели до
нужного размера — поэтому `OpenAIEmbeddingProvider` всегда запрашивает
`dimensions=QDRANT_VECTOR_SIZE`, и схема коллекции не меняется в
зависимости от провайдера.

Это решает только совместимость *схемы*, не совместимость *данных*:
векторы Voyage и OpenAI живут в разных пространствах, косинусное
сходство между старым (до переключения) вектором и новым не имеет
смысла, даже если размерность совпадает. Смена `EMBEDDING_PROVIDER` на
уже проиндексированном репозитории **требует полного реиндекса**
(`recreate_collection` + `worker/indexer.py`) — это не бесплатная смена
конфига, как для answer/rerank.

## Что нужно реализовать

`api/services/embeddings.py`:

1. `EmbeddingProvider(ABC)` — абстрактный класс с методами
   `embed_documents(texts) -> list[list[float]]` и
   `embed_query(text) -> list[float]`
2. `VoyageEmbeddingProvider` — существующая логика (HTTP + ретраи с
   бэкоффом на 429/5xx), вынесенная в класс
3. `OpenAIEmbeddingProvider` — `client.embeddings.create(model=...,
   input=batch, dimensions=QDRANT_VECTOR_SIZE)`, тот же батчинг
   (`MAX_BATCH_SIZE`), ретраи не переизобретаются — полагается на
   встроенный retry openai SDK (как и `OpenAIAnswerProvider`/
   `OpenAIRerankProvider`). `text-embedding-3-*` жёстко ограничивает вход
   8192 токенами (у Voyage `voyage-code-3` — 32000, поэтому раньше это
   не всплывало) — большие чанки (сгенерированный файл, минифицированный
   бандл) обрезаются через `tiktoken` (`cl100k_base`) до лимита вместо
   падения всего батча с `openai.BadRequestError`
4. `get_embedding_provider() -> EmbeddingProvider` — фабрика: читает
   `EMBEDDING_PROVIDER` (`voyage` по умолчанию) и `EMBEDDING_MODEL`
   (дефолт зависит от провайдера: `voyage-code-3` / `text-embedding-3-small`)
5. Публичные функции `embed_documents(texts)` / `embed_query(text)`
   остаются с той же сигнатурой — делегируют в
   `get_embedding_provider()`. `worker/indexer.py` и
   `api/services/retrieval.py` не трогать вообще
6. Новая переменная окружения: `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`
   (`OPENAI_API_KEY` уже существует)

## Критерии готовности

- [ ] По умолчанию (без `EMBEDDING_PROVIDER` в `.env`) используется Voyage
- [ ] `EMBEDDING_PROVIDER=openai` переключает на OpenAI без правок кода
- [ ] Оба провайдера возвращают векторы размера `QDRANT_VECTOR_SIZE`
- [ ] `EMBEDDING_MODEL` переопределяет модель у активного провайдера
- [ ] Неизвестный `EMBEDDING_PROVIDER` — понятная ошибка, а не тихий фолбэк
- [ ] Оба провайдера покрыты тестами с моками (реальные вызовы запрещены)
- [ ] `worker/indexer.py`, `api/services/retrieval.py` и их тесты не
      тронуты — контракт `embed_documents`/`embed_query` не изменился
- [ ] README/CLAUDE.md явно предупреждают: смена провайдера на уже
      проиндексированном репозитории требует полного реиндекса

## Не делать

- Не менять `QDRANT_VECTOR_SIZE` — оба провайдера подстраиваются под него
- Не убирать код Voyage — остаётся дефолтом в конфиге
- Не реализовывать автоматическую миграцию/реиндекс при смене провайдера —
  это ручное действие пользователя (кнопка «Переиндексировать»/
  `POST /reindex`, уже существует)
