import { useRef, useState, type FormEvent } from "react";
import { API_BASE_URL, sendFeedback } from "../api";
import { streamSse } from "../sse";
import type { ChatMessage, Repo, Source } from "../types";

function updateLastAssistant(messages: ChatMessage[], updater: (m: ChatMessage) => ChatMessage): ChatMessage[] {
  const lastIndex = messages.length - 1;
  return messages.map((m, i) => (i === lastIndex ? updater(m) : m));
}

export function ChatView({ repo, onBack }: { repo: Repo; onBack: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || sending) return;

    setQuestion("");
    setMessages((prev) => [...prev, { role: "user", content: q }, { role: "assistant", content: "" }]);
    setSending(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamSse(
        `${API_BASE_URL}/api/v1/repos/${repo.id}/ask`,
        { question: q },
        (event) => {
          // Each branch calls setMessages as soon as its event arrives —
          // this is what makes the answer stream in token by token
          // instead of appearing all at once when the request finishes.
          if (event.event === "sources") {
            const { sources } = event.data as { sources: Source[] };
            setMessages((prev) => updateLastAssistant(prev, (m) => ({ ...m, sources })));
          } else if (event.event === "token") {
            const { text } = event.data as { text: string };
            setMessages((prev) => updateLastAssistant(prev, (m) => ({ ...m, content: m.content + text })));
          } else if (event.event === "done") {
            const { query_id } = event.data as { query_id: string };
            setMessages((prev) => updateLastAssistant(prev, (m) => ({ ...m, queryId: query_id })));
          } else if (event.event === "error") {
            // The backend already sent sources/tokens and hit a failure
            // mid-stream (e.g. the LLM call itself failed) — it can't turn
            // this into a normal HTTP error status this late, so it sends
            // this event instead of just dropping the connection.
            const { message } = event.data as { message: string };
            setMessages((prev) =>
              updateLastAssistant(prev, (m) => ({ ...m, content: m.content || message, isError: true })),
            );
          }
        },
        controller.signal,
      );
    } catch (err) {
      setMessages((prev) =>
        updateLastAssistant(prev, (m) => ({
          ...m,
          content: m.content || `Error: ${err instanceof Error ? err.message : "unknown"}`,
          isError: true,
        })),
      );
    } finally {
      setSending(false);
    }
  }

  function rate(index: number, rating: 1 | -1) {
    const message = messages[index];
    if (!message.queryId) return;
    sendFeedback(message.queryId, rating).then(() => {
      setMessages((prev) => prev.map((m, i) => (i === index ? { ...m, feedback: rating } : m)));
    });
  }

  return (
    <div className="mx-auto flex h-screen max-w-3xl flex-col p-4">
      <header className="mb-4 flex items-center gap-3 border-b border-gray-200 pb-3">
        <button onClick={onBack} className="text-sm text-gray-500 hover:text-gray-900">
          ← Back
        </button>
        <h2 className="font-medium text-gray-900">{repo.repo_full_name}</h2>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto py-4">
        {messages.length === 0 && (
          <p className="text-sm text-gray-400">Ask a question about this repository's code.</p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "text-right" : "text-left"}>
            <div
              className={
                "inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-2 text-sm " +
                (m.role === "user"
                  ? "bg-gray-900 text-white"
                  : m.isError
                    ? "bg-red-50 text-red-700"
                    : "bg-gray-100 text-gray-900")
              }
            >
              {m.content || (m.role === "assistant" && sending && i === messages.length - 1 ? "…" : "")}
            </div>
            {m.sources && m.sources.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1 text-xs text-gray-500">
                {m.sources.map((s, si) => (
                  <span key={si} className="rounded border border-gray-200 bg-gray-50 px-2 py-0.5">
                    {s.file_path}:{s.start_line}-{s.end_line}
                  </span>
                ))}
              </div>
            )}
            {m.role === "assistant" && m.queryId && (
              <div className="mt-1 flex gap-2 text-xs">
                <button
                  onClick={() => rate(i, 1)}
                  className={m.feedback === 1 ? "text-green-600" : "text-gray-400 hover:text-gray-700"}
                >
                  👍
                </button>
                <button
                  onClick={() => rate(i, -1)}
                  className={m.feedback === -1 ? "text-red-600" : "text-gray-400 hover:text-gray-700"}
                >
                  👎
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      <form onSubmit={handleSubmit} className="mt-2 flex gap-2 border-t border-gray-200 pt-3">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask something about the code…"
          className="flex-1 rounded-lg border border-gray-300 px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-gray-900"
          disabled={sending}
        />
        <button
          type="submit"
          disabled={sending || !question.trim()}
          className="rounded-lg bg-gray-900 px-4 py-2 text-sm text-white transition disabled:opacity-40"
        >
          Send
        </button>
      </form>
    </div>
  );
}
