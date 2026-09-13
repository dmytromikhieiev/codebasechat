import httpx
import pytest
import respx

from api.services import embeddings


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_secret(key: str) -> str:
        return "test-voyage-key"

    monkeypatch.setattr(embeddings, "get_secret", fake_get_secret)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch):
    async def fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(embeddings.asyncio, "sleep", fake_sleep)


@pytest.fixture(autouse=True)
def _default_provider_env(monkeypatch: pytest.MonkeyPatch):
    # Tests must not depend on whatever EMBEDDING_PROVIDER happens to be set
    # in the running container's .env — force the (voyage) default unless a
    # test explicitly overrides it.
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)


@respx.mock
async def test_embed_documents_returns_vectors_in_order() -> None:
    respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})
    )

    vectors = await embeddings.embed_documents(["foo", "bar"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@respx.mock
async def test_embed_query_returns_single_vector() -> None:
    respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.5, 0.6]}]})
    )

    vector = await embeddings.embed_query("hello")

    assert vector == [0.5, 0.6]


@respx.mock
async def test_embed_documents_batches_large_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "MAX_BATCH_SIZE", 2)
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]}),
            httpx.Response(200, json={"data": [{"embedding": [3.0]}]}),
        ]
    )

    vectors = await embeddings.embed_documents(["a", "b", "c"])

    assert route.call_count == 2
    assert vectors == [[1.0], [2.0], [3.0]]


@respx.mock
async def test_embed_documents_retries_on_429_then_succeeds() -> None:
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"data": [{"embedding": [0.1]}]}),
        ]
    )

    vectors = await embeddings.embed_documents(["foo"])

    assert route.call_count == 3
    assert vectors == [[0.1]]


@respx.mock
async def test_embed_documents_retries_on_5xx() -> None:
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"data": [{"embedding": [0.2]}]})]
    )

    vectors = await embeddings.embed_documents(["foo"])

    assert route.call_count == 2
    assert vectors == [[0.2]]


@respx.mock
async def test_embed_documents_gives_up_after_max_retries() -> None:
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await embeddings.embed_documents(["foo"])

    assert route.call_count == embeddings.MAX_RETRIES + 1


@respx.mock
async def test_embed_documents_does_not_retry_on_400() -> None:
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(400, json={"error": "bad request"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await embeddings.embed_documents(["foo"])

    assert route.call_count == 1


@respx.mock
async def test_embed_documents_respects_retry_after_header() -> None:
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, json={}),
            httpx.Response(200, json={"data": [{"embedding": [0.3]}]}),
        ]
    )

    vectors = await embeddings.embed_documents(["foo"])

    assert route.call_count == 2
    assert vectors == [[0.3]]


# --- OpenAI provider --------------------------------------------------------


class _FakeEmbedding:
    def __init__(self, vector: list[float]):
        self.embedding = vector


class _FakeEmbeddingResponse:
    def __init__(self, vectors: list[list[float]]):
        self.data = [_FakeEmbedding(v) for v in vectors]


class _FakeOpenAIClient:
    def __init__(self, vectors: list[list[float]]):
        self.embeddings = self
        self._vectors = vectors
        self.create_kwargs: dict | None = None

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeEmbeddingResponse(self._vectors)


async def test_openai_provider_embed_documents_returns_vectors_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeOpenAIClient([[0.1, 0.2], [0.3, 0.4]])
    monkeypatch.setattr(embeddings.openai, "AsyncOpenAI", lambda **kwargs: fake_client)

    provider = embeddings.OpenAIEmbeddingProvider("text-embedding-3-small")
    vectors = await provider.embed_documents(["foo", "bar"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert fake_client.create_kwargs["dimensions"] == embeddings.QDRANT_VECTOR_SIZE
    assert fake_client.create_kwargs["model"] == "text-embedding-3-small"


async def test_openai_provider_embed_query_returns_single_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings.openai, "AsyncOpenAI", lambda **kwargs: _FakeOpenAIClient([[0.5, 0.6]]))

    provider = embeddings.OpenAIEmbeddingProvider("text-embedding-3-small")
    vector = await provider.embed_query("hello")

    assert vector == [0.5, 0.6]


async def test_openai_provider_truncates_oversized_input(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeOpenAIClient([[0.1]])
    monkeypatch.setattr(embeddings.openai, "AsyncOpenAI", lambda **kwargs: fake_client)

    provider = embeddings.OpenAIEmbeddingProvider("text-embedding-3-small")
    huge_text = "word " * 20000  # far more than OPENAI_MAX_INPUT_TOKENS
    await provider.embed_documents([huge_text])

    sent_text = fake_client.create_kwargs["input"][0]
    token_count = len(provider._encoding.encode(sent_text))
    assert token_count <= embeddings.OPENAI_MAX_INPUT_TOKENS
    assert len(sent_text) < len(huge_text)


async def test_openai_provider_leaves_small_input_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeOpenAIClient([[0.1]])
    monkeypatch.setattr(embeddings.openai, "AsyncOpenAI", lambda **kwargs: fake_client)

    provider = embeddings.OpenAIEmbeddingProvider("text-embedding-3-small")
    await provider.embed_documents(["a short chunk of code"])

    assert fake_client.create_kwargs["input"] == ["a short chunk of code"]


async def test_openai_provider_batches_large_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "MAX_BATCH_SIZE", 2)
    calls: list[list[str]] = []

    class _BatchTrackingClient(_FakeOpenAIClient):
        async def create(self, **kwargs):
            calls.append(kwargs["input"])
            return _FakeEmbeddingResponse([[float(len(calls))]] * len(kwargs["input"]))

    monkeypatch.setattr(embeddings.openai, "AsyncOpenAI", lambda **kwargs: _BatchTrackingClient([]))

    provider = embeddings.OpenAIEmbeddingProvider("text-embedding-3-small")
    vectors = await provider.embed_documents(["a", "b", "c"])

    assert calls == [["a", "b"], ["c"]]
    assert vectors == [[1.0], [1.0], [2.0]]


# --- Factory / config wiring -------------------------------------------------


def test_get_embedding_provider_defaults_to_voyage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    provider = embeddings.get_embedding_provider()

    assert isinstance(provider, embeddings.VoyageEmbeddingProvider)
    assert provider._model == embeddings.DEFAULT_MODELS["voyage"]


def test_get_embedding_provider_selects_openai_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    provider = embeddings.get_embedding_provider()

    assert isinstance(provider, embeddings.OpenAIEmbeddingProvider)
    assert provider._model == embeddings.DEFAULT_MODELS["openai"]


def test_get_embedding_provider_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "OpenAI")

    assert isinstance(embeddings.get_embedding_provider(), embeddings.OpenAIEmbeddingProvider)


def test_get_embedding_provider_respects_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyage-code-3-lite")

    provider = embeddings.get_embedding_provider()

    assert provider._model == "voyage-code-3-lite"


def test_get_embedding_provider_raises_on_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bogus")

    with pytest.raises(ValueError, match="bogus"):
        embeddings.get_embedding_provider()


async def test_embed_documents_delegates_to_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _FakeProvider(embeddings.EmbeddingProvider):
        async def embed_documents(self, texts):
            calls.append(texts)
            return [[0.0] for _ in texts]

        async def embed_query(self, text):
            raise AssertionError("not expected")

    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda: _FakeProvider())

    vectors = await embeddings.embed_documents(["a", "b"])

    assert vectors == [[0.0], [0.0]]
    assert calls == [["a", "b"]]


async def test_embed_query_delegates_to_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProvider(embeddings.EmbeddingProvider):
        async def embed_documents(self, texts):
            raise AssertionError("not expected")

        async def embed_query(self, text):
            return [9.0]

    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda: _FakeProvider())

    vector = await embeddings.embed_query("q")

    assert vector == [9.0]
