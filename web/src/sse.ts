/**
 * Server-Sent Events over a POST, parsed incrementally.
 *
 * `EventSource` cannot be used here: it only issues GETs, and both endpoints
 * take a body. So this reads the response stream by hand.
 *
 * The part that matters is that it never waits for the whole body. Anything
 * that accumulates the response before parsing -- `await response.text()`, a
 * buffering proxy, a dev server that collects the stream -- gives exactly the
 * batch behaviour the engine was built to avoid, and it would look like a
 * backend problem from here.
 */

import type { EngineEvent, EventName } from "./types.gen";

/** A frame boundary can fall anywhere, including between the \r and the \n. */
const FRAME = /\r\n\r\n|\n\n|\r\r/;

export interface StreamHandle {
  cancel: () => void;
}

export function streamEvents(
  url: string,
  body: unknown | undefined,
  onEvent: (event: EngineEvent) => void,
  onDone: (error?: Error) => void,
): StreamHandle {
  const controller = new AbortController();

  (async () => {
    let error: Error | undefined;
    try {
      const response = await fetch(url, {
        method: "POST",
        signal: controller.signal,
        headers: body === undefined ? {} : { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });

      if (!response.ok) {
        // The engine's own failures arrive as `error` events on a 200 stream.
        // A non-2xx here is the layer in front: no key configured, rate
        // limited, bad request. Surface the server's wording, which explains
        // what to do about it.
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`);
      }
      if (!response.body) throw new Error("this browser gave no readable stream");

      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += value;

        // Emit every complete frame and keep the remainder. A partial frame is
        // held rather than guessed at.
        for (;;) {
          const match = FRAME.exec(buffer);
          if (!match) break;
          const frame = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          const parsed = parseFrame(frame);
          if (parsed) onEvent(parsed);
        }
      }

      const tail = parseFrame(buffer);
      if (tail) onEvent(tail);
    } catch (exc) {
      if ((exc as Error)?.name !== "AbortError") error = exc as Error;
    } finally {
      onDone(error);
    }
  })();

  return { cancel: () => controller.abort() };
}

function parseFrame(frame: string): EngineEvent | null {
  let name: string | null = null;
  const dataLines: string[] = [];

  for (const line of frame.split(/\r\n|\n|\r/)) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (name === null || dataLines.length === 0) return null;

  try {
    // Multi-line data fields are joined with newlines, per the SSE grammar.
    return { name: name as EventName, data: JSON.parse(dataLines.join("\n")) } as EngineEvent;
  } catch {
    return null;
  }
}
