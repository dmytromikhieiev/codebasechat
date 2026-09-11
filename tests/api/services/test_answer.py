import uuid
from collections.abc import AsyncIterator

import pytest

from api.services import answer
from api.services.retrieval import RetrievedChunk


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_secret(key: str) -> str:
        return f"test-{key.lower()}"

    monkeypatch.setattr(answer, "get_secret", fake_get_secret)


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        id=uuid.uuid4(),
        file_path="a.py",
        start_line=1,
        end_line=2,
        function_name="foo",
        language="python",
        content="def foo():\n    return 1\n",
    )


# --- Anthropic provider ------------------------------------------------


class _FakeMessageStream:
    def __init__(self, tokens: list[str]):
        self._tokens = tokens

    async def __aenter__(self) -> "_FakeMessageStream":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    @property
    async def text_stream(self):
        for token in self._tokens:
            yield token


class _FakeAnthropicClient:
    def __init__(self, tokens: list[str]):
        self.messages = self
        self._tokens = tokens

    def stream(self, **kwargs):
        return _FakeMessageStream(self._tokens)


async def test_anthropic_provider_streams_tokens_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answer.anthropic, "AsyncAnthropic", lambda **kwargs: _FakeAnthropicClient(["Hello", " world"]))

    provider = answer.AnthropicAnswerProvider("claude-sonnet-5")
    tokens = [t async for t in provider.stream_answer("what does foo do?", [_chunk()])]

    assert tokens == ["Hello", " world"]


# --- OpenAI provider -----------------------------------------------------


class _FakeDelta:
    def __init__(self, content: str | None):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None):
        self.delta = _FakeDelta(content)


class _FakeChunk:
    def __init__(self, content: str | None):
        self.choices = [_FakeChoice(content)]


class _FakeOpenAIStream:
    def __init__(self, contents: list[str | None]):
        self._contents = contents

    def __aiter__(self) -> AsyncIterator[_FakeChunk]:
        return self._gen()

    async def _gen(self):
        for content in self._contents:
            yield _FakeChunk(content)


class _FakeOpenAIClient:
    def __init__(self, contents: list[str | None]):
        self.chat = self
        self.completions = self
        self._contents = contents

    async def create(self, **kwargs):
        return _FakeOpenAIStream(self._contents)


async def test_openai_provider_streams_tokens_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        answer.openai, "AsyncOpenAI", lambda **kwargs: _FakeOpenAIClient(["Hello", " world"])
    )

    provider = answer.OpenAIAnswerProvider("gpt-4o")
    tokens = [t async for t in provider.stream_answer("what does foo do?", [_chunk()])]

    assert tokens == ["Hello", " world"]


async def test_openai_provider_skips_chunks_without_content(monkeypatch: pytest.MonkeyPatch) -> None:
    # The first streamed chunk (role-only) and any keepalive chunks carry
    # delta.content = None — must not surface as literal "None" tokens.
    monkeypatch.setattr(
        answer.openai, "AsyncOpenAI", lambda **kwargs: _FakeOpenAIClient([None, "Hello", None, " world"])
    )

    provider = answer.OpenAIAnswerProvider("gpt-4o")
    tokens = [t async for t in provider.stream_answer("what does foo do?", [_chunk()])]

    assert tokens == ["Hello", " world"]


# --- Factory / config wiring ---------------------------------------------


def test_get_answer_provider_defaults_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANSWER_PROVIDER", raising=False)
    monkeypatch.delenv("ANSWER_MODEL", raising=False)

    provider = answer.get_answer_provider()

    assert isinstance(provider, answer.OpenAIAnswerProvider)
    assert provider._model == answer.DEFAULT_MODELS["openai"]


def test_get_answer_provider_selects_anthropic_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANSWER_PROVIDER", "anthropic")
    monkeypatch.delenv("ANSWER_MODEL", raising=False)

    provider = answer.get_answer_provider()

    assert isinstance(provider, answer.AnthropicAnswerProvider)
    assert provider._model == answer.DEFAULT_MODELS["anthropic"]


def test_get_answer_provider_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANSWER_PROVIDER", "Anthropic")

    assert isinstance(answer.get_answer_provider(), answer.AnthropicAnswerProvider)


def test_get_answer_provider_respects_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANSWER_PROVIDER", "openai")
    monkeypatch.setenv("ANSWER_MODEL", "gpt-4o-mini")

    provider = answer.get_answer_provider()

    assert provider._model == "gpt-4o-mini"


def test_get_answer_provider_raises_on_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANSWER_PROVIDER", "bogus")

    with pytest.raises(ValueError, match="bogus"):
        answer.get_answer_provider()


async def test_stream_answer_delegates_to_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _FakeProvider(answer.AnswerProvider):
        async def stream_answer(self, question, chunks) -> AsyncIterator[str]:
            calls.append((question, chunks))
            yield "ok"

    monkeypatch.setattr(answer, "get_answer_provider", lambda: _FakeProvider())

    tokens = [t async for t in answer.stream_answer("q", [_chunk()])]

    assert tokens == ["ok"]
    assert len(calls) == 1
