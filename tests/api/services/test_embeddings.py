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
