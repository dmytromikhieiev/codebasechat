import uuid

import pytest

from api.services import answer
from api.services.retrieval import RetrievedChunk


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


class _FakeMessages:
    def __init__(self, tokens: list[str]):
        self._tokens = tokens

    def stream(self, **kwargs):
        return _FakeMessageStream(self._tokens)


class _FakeAnthropicClient:
    def __init__(self, tokens: list[str]):
        self.messages = _FakeMessages(tokens)


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_secret(key: str) -> str:
        return "test-anthropic-key"

    monkeypatch.setattr(answer, "get_secret", fake_get_secret)


async def test_stream_answer_yields_tokens_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answer.anthropic, "AsyncAnthropic", lambda **kwargs: _FakeAnthropicClient(["Hello", " world"]))

    chunk = RetrievedChunk(
        id=uuid.uuid4(),
        file_path="a.py",
        start_line=1,
        end_line=2,
        function_name="foo",
        language="python",
        content="def foo():\n    return 1\n",
    )

    tokens = [token async for token in answer.stream_answer("what does foo do?", [chunk])]

    assert tokens == ["Hello", " world"]
