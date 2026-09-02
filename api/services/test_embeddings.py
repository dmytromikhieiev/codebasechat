import httpx
import pytest
import respx

from api.services import embeddings


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_secret(key: str) -> str:
        return "test-voyage-key"

    monkeypatch.setattr(embeddings, "get_secret", fake_get_secret)


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
