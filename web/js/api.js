// HTTP + SSE client.
//
// One parser for both streams. EventSource is deliberately not used: it is
// GET-only, so it cannot carry the /api/ask request body, and it cannot set
// headers - using it would mean two different stream-reading code paths.

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

export const health = () => getJSON('/api/health');
export const quote = (ticker) => getJSON(`/api/quote/${encodeURIComponent(ticker)}`);

export const tickers = (query, limit = 8) =>
  getJSON(`/api/tickers?q=${encodeURIComponent(query)}&limit=${limit}`);

export const resolve = (text, active) =>
  getJSON(`/api/resolve?q=${encodeURIComponent(text)}&active=${encodeURIComponent(active || '')}`);

/**
 * Read an SSE body, dispatching each event to `handlers[eventName]`.
 * Returns an object with abort(); resolves when the stream ends.
 */
function readStream(response, handlers, signal) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const dispatch = (frame) => {
    let event = 'message';
    const dataLines = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    let payload;
    try {
      payload = JSON.parse(dataLines.join('\n'));
    } catch {
      return; // a truncated frame is dropped rather than crashing the render
    }
    const handler = handlers[event];
    if (handler) handler(payload);
  };

  return (async () => {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf('\n\n')) !== -1) {
          dispatch(buffer.slice(0, split));
          buffer = buffer.slice(split + 2);
        }
      }
      if (buffer.trim()) dispatch(buffer);
    } catch (error) {
      if (signal && signal.aborted) return;
      if (handlers.streamerror) handlers.streamerror(error);
    }
  })();
}

export function briefStream(ticker, handlers) {
  const controller = new AbortController();
  const done = (async () => {
    let response;
    try {
      response = await fetch(`/api/brief/${encodeURIComponent(ticker)}`, {
        headers: { Accept: 'text/event-stream' },
        signal: controller.signal,
      });
    } catch (error) {
      if (!controller.signal.aborted && handlers.streamerror) handlers.streamerror(error);
      return;
    }
    if (!response.ok || !response.body) {
      if (handlers.streamerror) handlers.streamerror(new Error(`HTTP ${response.status}`));
      return;
    }
    await readStream(response, handlers, controller.signal);
  })();
  return { abort: () => controller.abort(), done };
}

export function askStream(body, handlers) {
  const controller = new AbortController();
  const done = (async () => {
    let response;
    try {
      response = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (error) {
      if (!controller.signal.aborted && handlers.streamerror) handlers.streamerror(error);
      return;
    }
    if (!response.ok || !response.body) {
      if (handlers.streamerror) handlers.streamerror(new Error(`HTTP ${response.status}`));
      return;
    }
    await readStream(response, handlers, controller.signal);
  })();
  return { abort: () => controller.abort(), done };
}
