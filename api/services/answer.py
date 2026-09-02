from collections.abc import AsyncIterator

import anthropic

from api.services.retrieval import RetrievedChunk
from api.services.secrets import get_secret

ANSWER_MODEL = "claude-sonnet-5"
MAX_ANSWER_TOKENS = 2048

SYSTEM_PROMPT = (
    "You are a code assistant answering questions about a specific codebase. "
    "Answer strictly from the provided code excerpts — if they don't contain "
    "the answer, say so plainly instead of guessing or using outside knowledge. "
    "When you reference code, cite the file path and line numbers from the "
    "excerpt headers."
)


async def stream_answer(question: str, chunks: list[RetrievedChunk]) -> AsyncIterator[str]:
    api_key = await get_secret("ANTHROPIC_API_KEY")
    client = anthropic.AsyncAnthropic(api_key=api_key)

    async with client.messages.stream(
        model=ANSWER_MODEL,
        max_tokens=MAX_ANSWER_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(question, chunks)}],
    ) as stream:
        async for text in stream.text_stream:
            yield text


def _build_user_message(question: str, chunks: list[RetrievedChunk]) -> str:
    excerpts = "\n\n".join(
        f"### {chunk.file_path}:{chunk.start_line}-{chunk.end_line}\n```{chunk.language}\n{chunk.content}\n```"
        for chunk in chunks
    )
    return f"Code excerpts:\n\n{excerpts}\n\nQuestion: {question}"
