# Задача: Конфигурируемый провайдер реранкинга (Voyage + OpenAI)

## Контекст

`api/services/reranker.py` жёстко использовал Voyage (`rerank-2`). По
аналогии с генерацией ответа (`api/services/answer.py`, см.
[[answer-provider-openai]]) нужен полиморфный выбор провайдера реранкинга
через конфиг — Voyage остаётся дефолтом (это специализированный
cross-encoder, ничего не сломано), OpenAI добавляется как alternative.

Важное отличие от `answer.py`: у OpenAI нет отдельного rerank-эндпоинта
(в отличие от Voyage/Cohere). `OpenAIRerankProvider` вместо этого
просит чат-модель (`gpt-4o-mini` по умолчанию) оценить релевантность
каждого сниппета через structured output (`response_format:
json_schema`) и возвращает индексы с score. Это медленнее и дороже на
вызов, чем специализированный кросс-энкодер — сознательный компромисс
ради возможности не заводить отдельный ключ Voyage.

Затрагивает только реранкинг. Эмбеддинги остаются на Voyage
(`voyage-code-3`) без изменений — смена провайдера эмбеддингов меняет
размерность вектора и требует полной переиндексации, это отдельная
задача.

## Что нужно реализовать

`api/services/reranker.py`:

1. `RerankProvider(ABC)` — абстрактный класс с одним методом
   `rerank(query, chunks, top_k) -> list[RetrievedChunk]`
2. `VoyageRerankProvider` — существующая логика (HTTP-вызов
   `POST /v1/rerank`), вынесенная в класс
3. `OpenAIRerankProvider` — чат-модель + `response_format=json_schema`
   (`{"results": [{"index": int, "relevance_score": float}, ...]}`),
   сортировка по score, top_k, индексы вне диапазона отбрасываются
4. `get_rerank_provider() -> RerankProvider` — фабрика: читает
   `RERANK_PROVIDER` (`voyage` по умолчанию) и `RERANK_MODEL` (дефолт
   зависит от провайдера: `rerank-2` / `gpt-4o-mini`) из окружения
5. Публичная функция `rerank(query, chunks, top_k=5)` остаётся с той же
   сигнатурой и продолжает сама коротко замыкать пустой список
   чанков — делегирует в `get_rerank_provider()` иначе.
   `api/routers/ask.py` менять не нужно вообще
6. Новые переменные окружения: `RERANK_PROVIDER`, `RERANK_MODEL`
   (`OPENAI_API_KEY` уже существует из задачи про answer-provider)

## Критерии готовности

- [ ] По умолчанию (без `RERANK_PROVIDER` в `.env`) используется Voyage
- [ ] `RERANK_PROVIDER=openai` переключает на OpenAI без правок кода
- [ ] `RERANK_MODEL` переопределяет модель у активного провайдера
- [ ] Неизвестный `RERANK_PROVIDER` — понятная ошибка, а не тихий фолбэк
- [ ] Оба провайдера покрыты тестами с моками (реальные вызовы запрещены)
- [ ] `api/routers/ask.py` и его тесты не тронуты — контракт `rerank`
      не изменился

## Не делать

- Не трогать эмбеддинги — остаются на Voyage `voyage-code-3`
- Не убирать код Voyage — остаётся дефолтом в конфиге
- Не менять дефолтную модель для OpenAI answer-провайдера заодно
