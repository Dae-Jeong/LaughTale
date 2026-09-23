import assert from "node:assert/strict";
import test from "node:test";
import { browserTransport } from "../src/chat/api";

test("HTTP uses API cookies while WS uses the separate same-host Gateway", async (t) => {
  let httpUrl = "", socketUrl = "";
  let credentials: RequestCredentials | undefined;
  t.mock.method(globalThis, "fetch", async (url: URL, options: RequestInit) => {
    httpUrl = url.href; credentials = options.credentials;
    return new Response(JSON.stringify({ data: [] }));
  });
  const original = globalThis.WebSocket;
  class Socket { constructor(url: URL) { socketUrl = url.href; } }
  globalThis.WebSocket = Socket as unknown as typeof WebSocket;
  t.after(() => { globalThis.WebSocket = original; });
  const transport = browserTransport("http://127.0.0.1:18082", "http://127.0.0.1:18085");
  await transport.request("/v1/session", { signal: new AbortController().signal }); transport.socket();
  assert.equal(httpUrl, "http://127.0.0.1:18082/v1/session");
  assert.equal(credentials, "include");
  assert.equal(socketUrl, "ws://127.0.0.1:18085/v1/ws");
  browserTransport("https://example.test").socket();
  assert.equal(socketUrl, "wss://example.test/v1/ws");
});

test("split endpoints retain host cookie and TLS boundaries", () => {
  for (const gateway of ["http://localhost:18085", "https://127.0.0.1:18085", "file:///tmp/ws", "http://name:secret@127.0.0.1"]) {
    assert.throws(() => browserTransport("http://127.0.0.1:18082", gateway));
  }
});
