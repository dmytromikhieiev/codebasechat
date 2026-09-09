import uuid

import httpx
import pytest
import respx

from api.services import reranker
from api.services.retrieval import RetrievedChunk


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_secret(key: str) -> str:
        return "test-voyage-key"

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


@respx.mock
async def test_rerank_reorders_and_attaches_scores() -> None:
    chunks = [_chunk(0), _chunk(1), _chunk(2)]
    respx.post("https://api.voyageai.com/v1/rerank").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]},
        )
    )

    result = await reranker.rerank("query", chunks, top_k=2)

    assert [c.file_path for c in result] == ["2.py", "0.py"]
    assert result[0].score == 0.9
    assert result[1].score == 0.5


async def test_rerank_empty_chunks_skips_http_call() -> None:
    result = await reranker.rerank("query", [])

    assert result == []
