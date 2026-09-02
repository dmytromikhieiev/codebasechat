export interface SseEvent {
  event: string;
  data: unknown;
}

/**
 * Manually parses a `text/event-stream` response from a POST request.
 * The browser's native EventSource only supports GET, but /ask takes a
 * JSON body, so this reads the stream chunk by chunk and calls onEvent
 * as soon as each `event: ...\ndata: ...\n\n` block completes — the
 * caller must update UI state inside onEvent for tokens to render as
 * they arrive, not after the stream closes.
 */
export async function streamSse(
  url: string,
  body: unknown,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => "");
    throw new Error(`request failed: ${response.status} ${text}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex = buffer.indexOf("\n\n");
    while (separatorIndex !== -1) {
      const rawEvent = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);
      const event = parseEvent(rawEvent);
      if (event) onEvent(event);
      separatorIndex = buffer.indexOf("\n\n");
    }
  }
}

function parseEvent(raw: string): SseEvent | null {
  let eventName = "message";
  let dataLine = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) {
      eventName = line.slice("event: ".length);
    } else if (line.startsWith("data: ")) {
      dataLine = line.slice("data: ".length);
    }
  }
  if (!dataLine) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLine) };
  } catch {
    return null;
  }
}
