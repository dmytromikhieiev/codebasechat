import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import anthropic
import openai

from api.services.retrieval import RetrievedChunk
from api.services.secrets import get_secret

MAX_ANSWER_TOKENS = 2048

SYSTEM_PROMPT = (
    "You are a code assistant answering questions about a specific codebase. "
    "Answer strictly from the provided code excerpts — if they don't contain "
    "the answer, say so plainly instead of guessing or using outside knowledge. "
    "When you reference code, cite the file path and line numbers from the "
    "excerpt headers."
)

# Used when ANSWER_MODEL isn't set — the model still always comes from
# config, this is just what "unconfigured" resolves to per provider.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}


class AnswerProvider(ABC):
    """One implementation per LLM backend. Which one is active is a config
    choice (see get_answer_provider), not something callers pick."""

    @abstractmethod
    def stream_answer(self, question: str, chunks: list[RetrievedChunk]) -> AsyncIterator[str]:
        """Yields the answer text piece by piece."""


class AnthropicAnswerProvider(AnswerProvider):
    def __init__(self, model: str) -> None:
        self._model = model

    async def stream_answer(self, question: str, chunks: list[RetrievedChunk]) -> AsyncIterator[str]:
        api_key = await get_secret("ANTHROPIC_API_KEY")
        client = anthropic.AsyncAnthropic(api_key=api_key)

        async with client.messages.stream(
            model=self._model,
            max_tokens=MAX_ANSWER_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_message(question, chunks)}],
        ) as stream:
            async for text in stream.text_stream:
                yield text


class OpenAIAnswerProvider(AnswerProvider):
    def __init__(self, model: str) -> None:
        self._model = model

    async def stream_answer(self, question: str, chunks: list[RetrievedChunk]) -> AsyncIterator[str]:
        api_key = await get_secret("OPENAI_API_KEY")
        client = openai.AsyncOpenAI(api_key=api_key)

        stream = await client.chat.completions.create(
            model=self._model,
            max_tokens=MAX_ANSWER_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(question, chunks)},
            ],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


_PROVIDERS: dict[str, type[AnswerProvider]] = {
    "anthropic": AnthropicAnswerProvider,
    "openai": OpenAIAnswerProvider,
}


def get_answer_provider() -> AnswerProvider:
    provider_name = os.environ.get("ANSWER_PROVIDER", "openai").lower()
    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ValueError(f"Unknown ANSWER_PROVIDER {provider_name!r}, expected one of {sorted(_PROVIDERS)}")

    model = os.environ.get("ANSWER_MODEL") or DEFAULT_MODELS[provider_name]
    return provider_cls(model)


async def stream_answer(question: str, chunks: list[RetrievedChunk]) -> AsyncIterator[str]:
    provider = get_answer_provider()
    async for text in provider.stream_answer(question, chunks):
        yield text


def _build_user_message(question: str, chunks: list[RetrievedChunk]) -> str:
    excerpts = "\n\n".join(
        f"### {chunk.file_path}:{chunk.start_line}-{chunk.end_line}\n```{chunk.language}\n{chunk.content}\n```"
        for chunk in chunks
    )
    return f"Code excerpts:\n\n{excerpts}\n\nQuestion: {question}"
