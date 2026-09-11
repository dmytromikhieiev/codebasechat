# Задача: Конфигурируемый провайдер генерации ответа (OpenAI + Anthropic)

## Контекст

`api/services/answer.py` жёстко использовал Anthropic (`claude-sonnet-5`).
Нужен полиморфный выбор провайдера через конфиг — OpenAI по умолчанию,
Anthropic остаётся доступен как альтернатива, без переключения в коде.

Затрагивает только генерацию ответа. Эмбеддинги и реранкинг остаются на
Voyage AI без изменений — смена их провайдера меняет размерность вектора
и требует полной переиндексации всех репозиториев, это отдельная задача.

## Что нужно реализовать

`api/services/answer.py`:

1. `AnswerProvider(ABC)` — абстрактный класс с одним методом
   `stream_answer(question, chunks) -> AsyncIterator[str]`
2. `AnthropicAnswerProvider` / `OpenAIAnswerProvider` — конкретные
   реализации, каждая берёт свой ключ через `secrets.get_secret`
   (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) и свою модель в конструкторе
3. `get_answer_provider() -> AnswerProvider` — фабрика: читает
   `ANSWER_PROVIDER` (`openai` по умолчанию) и `ANSWER_MODEL` (дефолт
   зависит от провайдера) из окружения
4. Публичная функция `stream_answer(question, chunks)` остаётся с той же
   сигнатурой — просто делегирует в `get_answer_provider()` — `api/routers/ask.py`
   менять не нужно вообще
5. Новые переменные окружения: `ANSWER_PROVIDER`, `ANSWER_MODEL`,
   `OPENAI_API_KEY`

## Критерии готовности

- [ ] По умолчанию (без `ANSWER_PROVIDER` в `.env`) используется OpenAI
- [ ] `ANSWER_PROVIDER=anthropic` переключает на Anthropic без правок кода
- [ ] `ANSWER_MODEL` переопределяет модель у активного провайдера
- [ ] Неизвестный `ANSWER_PROVIDER` — понятная ошибка, а не тихий фолбэк
- [ ] Оба провайдера покрыты тестами с моками (реальные вызовы запрещены)
- [ ] `api/routers/ask.py` и его тесты не тронуты — контракт `stream_answer`
      не изменился

## Не делать

- Не трогать `embeddings.py`/`reranker.py` — они остаются на Voyage
- Не убирать код Anthropic — оставляем как выбор в конфиге
