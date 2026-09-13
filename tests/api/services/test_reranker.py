import json
import uuid

import httpx
import pytest
import respx

from api.services import reranker
from api.services.retrieval import RetrievedChunk


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_secret(key: str) -> str:
        return f"test-{key.lower()}"

    monkeypatch.setattr(reranker, "get_secret", fake_get_secret)


def _chunk(index: int) -> RetrievedChunk:
    return RetrievedChunk(
        id=uuid.uuid4(),
        file_path=f"{index}.py",
        start_line=1,
        end_line=1,
        function_name=None,
        language="python",
        content=f"chunk {index}",
    )


# --- Voyage provider -------------------------------------------------------


@respx.mock
async def test_voyage_provider_reorders_and_attaches_scores() -> None:
    chunks = [_chunk(0), _chunk(1), _chunk(2)]
    respx.post("https://api.voyageai.com/v1/rerank").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]},
        )
    )

    provider = reranker.VoyageRerankProvider("rerank-2")
    result = await provider.rerank("query", chunks, top_k=2)

    assert [c.file_path for c in result] == ["2.py", "0.py"]
    assert result[0].score == 0.9
    assert result[1].score == 0.5


# --- OpenAI provider --------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeOpenAIClient:
    def __init__(self, content: str):
        self.chat = self
        self.completions = self
        self._content = content

    async def create(self, **kwargs):
        return _FakeCompletion(self._content)


async def test_openai_provider_reorders_and_attaches_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_chunk(0), _chunk(1), _chunk(2)]
    payload = json.dumps(
        {
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 2, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.5},
            ]
        }
    )
    monkeypatch.setattr(reranker.openai, "AsyncOpenAI", lambda **kwargs: _FakeOpenAIClient(payload))

    provider = reranker.OpenAIRerankProvider("gpt-4o-mini")
    result = await provider.rerank("query", chunks, top_k=2)

    assert [c.file_path for c in result] == ["2.py", "1.py"]
    assert result[0].score == 0.9
    assert result[1].score == 0.5


async def test_openai_provider_ignores_out_of_range_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_chunk(0), _chunk(1)]
    payload = json.dumps(
        {"results": [{"index": 5, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]}
    )
    monkeypatch.setattr(reranker.openai, "AsyncOpenAI", lambda **kwargs: _FakeOpenAIClient(payload))

    provider = reranker.OpenAIRerankProvider("gpt-4o-mini")
    result = await provider.rerank("query", chunks, top_k=2)

    assert [c.file_path for c in result] == ["0.py"]


# --- Factory / config wiring -------------------------------------------------


def test_get_rerank_provider_defaults_to_voyage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANK_PROVIDER", raising=False)
    monkeypatch.delenv("RERANK_MODEL", raising=False)

    provider = reranker.get_rerank_provider()

    assert isinstance(provider, reranker.VoyageRerankProvider)
    assert provider._model == reranker.DEFAULT_MODELS["voyage"]


def test_get_rerank_provider_selects_openai_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "openai")
    monkeypatch.delenv("RERANK_MODEL", raising=False)

    provider = reranker.get_rerank_provider()

    assert isinstance(provider, reranker.OpenAIRerankProvider)
    assert provider._model == reranker.DEFAULT_MODELS["openai"]


def test_get_rerank_provider_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "OpenAI")

    assert isinstance(reranker.get_rerank_provider(), reranker.OpenAIRerankProvider)


def test_get_rerank_provider_respects_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "voyage")
    monkeypatch.setenv("RERANK_MODEL", "rerank-2-lite")

    provider = reranker.get_rerank_provider()

    assert provider._model == "rerank-2-lite"


def test_get_rerank_provider_raises_on_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "bogus")

    with pytest.raises(ValueError, match="bogus"):
        reranker.get_rerank_provider()


async def test_rerank_delegates_to_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _FakeProvider(reranker.RerankProvider):
        async def rerank(self, query, chunks, top_k):
            calls.append((query, chunks, top_k))
            return chunks

    monkeypatch.setattr(reranker, "get_rerank_provider", lambda: _FakeProvider())

    chunks = [_chunk(0)]
    result = await reranker.rerank("q", chunks, top_k=3)

    assert result == chunks
    assert calls == [("q", chunks, 3)]


async def test_rerank_empty_chunks_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail():
        raise AssertionError("should not be called for empty chunks")

    monkeypatch.setattr(reranker, "get_rerank_provider", _fail)

    result = await reranker.rerank("query", [])

    assert result == []
