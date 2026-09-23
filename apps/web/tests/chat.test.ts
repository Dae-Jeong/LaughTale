import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import fixtures from "../../../contracts/chat/fixtures.json";
import { ApiError, browserTransport, type RequestResult, type Transport } from "../src/chat/api";
import { ChatController } from "../src/chat/controller";
import { emptyRoom, markDelivery, mergeMessages, needsRecovery, retryDraft, sequence, type Message, type Outgoing } from "../src/chat/model";
import { parseActor, parseFrame, parseHistory, parseRooms, parseStored } from "../src/chat/wire";

const base = fixtures.history.data[0];
const roomId = base.conversation_id;
const key = base.client_message_id;
const message = (seq: number, changes: Partial<Message> = {}): Message => ({ ...base, seq: String(seq),
  message_id: `00000000-0000-4000-8000-${String(seq + 100).padStart(12, "0")}`,
  client_message_id: `00000000-0000-4000-8000-${String(seq + 200).padStart(12, "0")}`, ...changes });
const history = (items: Message[], cursor = items.at(-1)?.seq ?? "0", head = cursor, hasMore = false) =>
  ({ data: items, meta: { next_cursor: cursor, snapshot_head_seq: head, has_more: hasMore } });
const stored = (item: Message) => ({ data: { ...item, state: "stored" } });
const created = (item: Message) => ({ type: "message.created", schema_version: 1, event_id: item.message_id, message: item });

test("provider-generated HTTP and WS fixtures satisfy consumer boundaries", () => {
  assert.equal(parseActor(fixtures.session).user_id, base.sender_id);
  assert.equal(parseRooms(fixtures.conversations)[0].conversation_id, roomId);
  assert.deepEqual(parseStored(fixtures.stored), base);
  assert.deepEqual(parseHistory(fixtures.history).data, [base]);
  for (const frame of [fixtures.subscribed, fixtures.message_created, fixtures.heads, fixtures.resync_required, fixtures.error]) assert.equal(parseFrame(frame).type, frame.type);
});
test("invalid wire values fail rather than becoming empty success", () => {
  assert.throws(() => parseHistory({ data: [] }));
  assert.throws(() => parseStored({ data: base }));
  assert.throws(() => parseFrame({ ...fixtures.subscribed, protocol_version: 2 }));
  assert.throws(() => parseFrame({ ...fixtures.message_created, event_id: key }));
  assert.throws(() => parseStored(stored({ ...base, sender_id: "user_a" })));
  assert.throws(() => parseStored(stored({ ...base, seq: "0" })));
  assert.throws(() => parseStored(stored({ ...base, created_at: "2026-09-08T00:00:00" })));
  assert.throws(() => parseStored(stored({ ...base, text: " " })));
});
test("decimal sequence guards preserve bigint precision and reject noncanonical or oversized input", () => {
  assert.equal(sequence("9007199254740993"), "9007199254740993");
  assert.equal(sequence("9223372036854775807"), "9223372036854775807");
  for (const bad of [1, -1, "-1", "01", "1.5", "1e3", "9223372036854775808", "1".repeat(100_000)]) assert.throws(() => sequence(bad));
});
test("out-of-order notifications are buffered until the contiguous gap is filled", () => {
  let room = mergeMessages(emptyRoom(roomId), [message(2)]);
  assert.equal(room.contiguous, "0");
  assert.equal(room.messages.length, 0);
  assert.equal(needsRecovery(room), true);
  room = mergeMessages(room, [message(1)]);
  assert.deepEqual(room.messages.map((item) => item.seq), ["1", "2"]);
  assert.equal(room.contiguous, "2");
  assert.deepEqual(room.buffer, {});
  room = mergeMessages(room, [message(1), message(2)]);
  assert.equal(room.messages.length, 2);
});
test("contiguous advancement remains exact above Number.MAX_SAFE_INTEGER", () => {
  const room = mergeMessages({ ...emptyRoom(roomId), contiguous: "9007199254740992", head: "9007199254740992" },
    [message(1, { seq: "9007199254740993" })]);
  assert.equal(room.contiguous, "9007199254740993");
  assert.equal(room.messages[0].seq, "9007199254740993");
});
test("HTTP transport includes credentials for both session reads and JSON writes", async (t) => {
  const requests: { url: string; init?: RequestInit }[] = [];
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ url: input.toString(), init });
    return new Response(JSON.stringify(fixtures.session), { headers: { "Content-Type": "application/json", "X-Request-ID": "fixture-request" } });
  });
  const transport = browserTransport("http://127.0.0.1:18082");
  const signal = new AbortController().signal;
  const read = await transport.request("/v1/session", { signal });
  await transport.request("/v1/dev/session", { signal, body: fixtures.session_request });
  assert.equal(read.requestId, "fixture-request");
  assert.deepEqual(requests.map((item) => item.init?.credentials), ["include", "include"]);
  assert.equal(requests[0].init?.method, "GET");
  assert.equal(requests[1].init?.method, "POST");
  assert.equal(requests[1].init?.body, JSON.stringify(fixtures.session_request));
  assert.equal(requests[1].url, "http://127.0.0.1:18082/v1/dev/session");
});
test("room and immutable identity collisions fail without changing the existing state", () => {
  const original = mergeMessages(emptyRoom(roomId), [message(1)]);
  for (const changed of [message(1, { text: "다른 본문" }), message(2, { message_id: message(1).message_id }),
    message(2, { client_message_id: message(1).client_message_id }), message(2, { conversation_id: key })]) {
    assert.throws(() => mergeMessages(original, [changed]));
    assert.equal(original.contiguous, "1");
  }
  assert.throws(() => mergeMessages(emptyRoom(roomId), [message(1), message(2, { conversation_id: key })]));
});
test("live buffer is bounded and retains a head to recover dropped events", () => {
  const room = mergeMessages(emptyRoom(roomId), Array.from({ length: 600 }, (_, i) => message(i + 2)));
  assert.equal(Object.keys(room.buffer).length, 500);
  assert.equal(room.head, "601");
  assert.equal(room.contiguous, "0");
});
test("retry preserves UUID and body, and a late HTTP error cannot downgrade WS-confirmed stored", () => {
  const outgoing: Outgoing = { ...base, state: "unknown" };
  let room = { ...emptyRoom(roomId), outgoing: [outgoing] };
  assert.equal(retryDraft(room, key).client_message_id, key);
  assert.equal(retryDraft(room, key).text, base.text);
  room = mergeMessages(room, [base]);
  room = markDelivery(room, key, "unknown");
  assert.equal(room.outgoing[0].state, "stored");
  assert.throws(() => retryDraft(room, key));
});

class FakeSocket {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  sent: unknown[] = [];
  send(value: string) { this.sent.push(JSON.parse(value)); }
  close(code = 1000) { this.readyState = 3; this.onclose?.({ code }); }
  open() { this.readyState = 1; this.onopen?.(); }
  emit(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
type Call = { path: string; body?: unknown; signal: AbortSignal };
function harness(custom?: (call: Call) => Promise<RequestResult | undefined>) {
  const calls: Call[] = [];
  const sockets: FakeSocket[] = [];
  const transport: Transport = {
    async request(path, options) {
      const call = { path, ...options };
      calls.push(call);
      const result = await custom?.(call);
      if (result) return result;
      if (path === "/v1/session" || path === "/v1/dev/session") return { body: fixtures.session };
      if (path === "/v1/internal-conversations") return { body: { data: [{ ...fixtures.conversations.data[0], head_seq: "0" }] } };
      return { body: history([]) };
    },
    socket() { const socket = new FakeSocket(); sockets.push(socket); return socket as unknown as WebSocket; },
  };
  return { calls, sockets, controller: new ChatController(transport, () => key) };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = async () => { await setImmediate(); await setImmediate(); };

test("subscribe precedes history and buffered live data merges with snapshot pages", async (t) => {
  const gate = deferred<RequestResult>();
  const h = harness(async ({ path }) => path.includes("/messages?") ? gate.promise : undefined);
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const socket = h.sockets[0];
  socket.open();
  assert.deepEqual(socket.sent, [fixtures.subscribe]);
  assert.equal(h.calls.filter((call) => call.path.includes("messages")).length, 0);
  socket.emit(fixtures.subscribed);
  socket.emit(created(message(2)));
  assert.equal(h.controller.getSnapshot().roomStates[roomId].contiguous, "0");
  gate.resolve({ body: history([message(1)], "1", "1") });
  await tick();
  assert.deepEqual(h.controller.getSnapshot().roomStates[roomId].messages.map((item) => item.seq), ["1", "2"]);
  assert.equal(h.controller.getSnapshot().roomStates[roomId].syncing, false);
});
test("authoritative heads discover a lost tail without a new message, and focus discovers head", async (t) => {
  let current = 0;
  const h = harness(async ({ path }) => path.includes("messages?") ? { body: current ? history([message(1)]) : history([]) } : undefined);
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const socket = h.sockets[0]; socket.open(); socket.emit({ ...fixtures.subscribed, head_seq: "0" }); await tick();
  current = 1;
  socket.emit(fixtures.heads);
  await tick();
  assert.equal(h.controller.getSnapshot().roomStates[roomId].contiguous, "1");
  current = 0;
  h.controller.focus();
  await tick();
  assert.ok(h.calls.at(-1)?.path.includes("after_seq=1"));
  assert.equal(h.calls.at(-1)?.path.includes("snapshot_head_seq"), false);
});
test("lost ACK retries the exact same UUID/body and history/WS/ACK render once", async (t) => {
  let attempts = 0;
  const h = harness(async ({ path, body }) => {
    if (path.endsWith("/messages") && body) {
      attempts += 1;
      if (attempts === 1) throw new Error("test-only lost ACK");
      return { body: fixtures.stored };
    }
  });
  t.after(() => h.controller.dispose());
  await h.controller.start();
  await h.controller.send(base.text);
  assert.equal(h.controller.getSnapshot().roomStates[roomId].outgoing[0].state, "unknown");
  await h.controller.retry(key);
  const sends = h.calls.filter((call) => call.path.endsWith("/messages"));
  assert.deepEqual(sends[0].body, sends[1].body);
  h.sockets[0].emit(fixtures.message_created);
  assert.equal(h.controller.getSnapshot().roomStates[roomId].messages.length, 1);
  assert.equal(h.controller.getSnapshot().roomStates[roomId].outgoing[0].state, "stored");
});
test("WS stored confirmation survives a later HTTP timeout; definite rejection is distinct", async (t) => {
  const gate = deferred<RequestResult>();
  const h = harness(async ({ path, body }) => path.endsWith("/messages") && body ? gate.promise : undefined);
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const sent = h.controller.send(base.text);
  h.sockets[0].emit(fixtures.message_created);
  gate.reject(new Error("test-only timeout"));
  await sent;
  assert.equal(h.controller.getSnapshot().roomStates[roomId].outgoing[0].state, "stored");
  const denied = harness(async ({ body, path }) => { if (body && path.endsWith("/messages")) throw new ApiError(409, "IDEMPOTENCY_CONFLICT"); return undefined; });
  t.after(() => denied.controller.dispose());
  await denied.controller.start(); await denied.controller.send("충돌 테스트");
  assert.equal(denied.controller.getSnapshot().roomStates[roomId].outgoing[0].state, "rejected");
});
test("user switch aborts requests, closes old socket and ignores late history/send/event", async (t) => {
  const gate = deferred<RequestResult>();
  const h = harness(async ({ path, body }) => {
    if (path.includes("/messages")) return gate.promise;
    if (path === "/v1/dev/session" && body) return { body: { data: { user_id: "00000000-0000-4000-8000-000000000002", display_name: "User B" } } };
  });
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const old = h.sockets[0]; old.open(); old.emit(fixtures.subscribed);
  const sent = h.controller.send(base.text);
  const oldRequests = h.calls.filter((call) => call.path.includes("messages"));
  await h.controller.switchUser("user_b");
  assert.equal(old.readyState, 3);
  assert.ok(oldRequests.every((call) => call.signal.aborted));
  old.emit(fixtures.message_created);
  gate.resolve({ body: fixtures.history });
  await sent; await tick();
  assert.equal(h.controller.getSnapshot().actor?.display_name, "User B");
  assert.equal(h.controller.getSnapshot().roomStates[roomId].messages.length, 0);
  assert.equal(h.controller.getSnapshot().roomStates[roomId].outgoing.length, 0);
});
test("concurrent persona requests are serialized so last selected cookie actor wins", async (t) => {
  const first = deferred<RequestResult>();
  const h = harness(async ({ path, body }) => {
    if (path !== "/v1/dev/session") return;
    if ((body as { user: string }).user === "user_a") return first.promise;
    return { body: { data: { ...fixtures.session.data, display_name: "User B" } } };
  });
  t.after(() => h.controller.dispose());
  const a = h.controller.switchUser("user_a"); await tick();
  const b = h.controller.switchUser("user_b");
  assert.equal(h.calls.filter((call) => call.path === "/v1/dev/session").length, 1);
  first.resolve({ body: fixtures.session });
  await Promise.all([a, b]);
  assert.equal(h.controller.getSnapshot().actor?.display_name, "User B");
});
test("history sequence and snapshot violations do not advance the cursor", async (t) => {
  const h = harness(async ({ path }) => path.includes("messages?") ? { body: history([message(2)], "2", "2") } : undefined);
  t.after(() => h.controller.dispose());
  await h.controller.start(); h.sockets[0].open(); h.sockets[0].emit({ ...fixtures.subscribed, head_seq: "2" }); await tick();
  const room = h.controller.getSnapshot().roomStates[roomId];
  assert.equal(room.contiguous, "0");
  assert.match(room.error ?? "", /순서/);
});

test("snapshot pagination stays fixed while a newer live event is buffered", async (t) => {
  const h = harness(async ({ path }) => {
    if (!path.includes("messages?")) return;
    const query = new URL(path, "http://test.invalid").searchParams;
    if (query.get("after_seq") === "0") return { body: history([message(1)], "1", "2", true) };
    assert.equal(query.get("snapshot_head_seq"), "2");
    return { body: history([message(2)], "2", "2") };
  });
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const socket = h.sockets[0]; socket.open(); socket.emit({ ...fixtures.subscribed, head_seq: "2" });
  socket.emit(created(message(3)));
  await tick();
  assert.deepEqual(h.controller.getSnapshot().roomStates[roomId].messages.map((item) => item.seq), ["1", "2", "3"]);
  assert.equal(h.calls.filter((call) => call.path.includes("messages?")).length, 2);
});
test("room switch unsubscribes and isolates cursors even when previous history finishes late", async (t) => {
  const other = "00000000-0000-4000-8000-000000000011";
  const first = deferred<RequestResult>();
  const h = harness(async ({ path }) => {
    if (path === "/v1/internal-conversations") return { body: { data: [
      fixtures.conversations.data[0], { ...fixtures.conversations.data[0], conversation_id: other, title: "두 번째 방" },
    ] } };
    if (path.includes(`${roomId}/messages?`)) return first.promise;
    if (path.includes(`${other}/messages?`)) return { body: history([message(1, { conversation_id: other })]) };
  });
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const socket = h.sockets[0]; socket.open(); socket.emit(fixtures.subscribed);
  h.controller.selectRoom(other);
  assert.deepEqual(socket.sent.slice(-2), [{ type: "unsubscribe", conversation_id: roomId }, { type: "subscribe", conversation_id: other }]);
  socket.emit({ ...fixtures.subscribed, conversation_id: other });
  first.resolve({ body: history([message(1)]) });
  await tick();
  assert.equal(h.controller.getSnapshot().selected, other);
  assert.equal(h.controller.getSnapshot().roomStates[other].contiguous, "1");
  assert.ok(h.controller.getSnapshot().roomStates[other].messages.every((item) => item.conversation_id === other));
});
test("manual reconnect closes the old socket and subscribes before resuming from contiguous cursor", async (t) => {
  const h = harness(async ({ path }) => path.includes("messages?") ? { body: history([message(1)]) } : undefined);
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const old = h.sockets[0]; old.open(); old.emit(fixtures.subscribed); await tick();
  h.controller.reconnect();
  assert.equal(old.readyState, 3);
  const socket = h.sockets[1]; socket.open();
  const calls = h.calls.length;
  assert.deepEqual(socket.sent, [fixtures.subscribe]);
  old.emit(created(message(2)));
  assert.equal(h.controller.getSnapshot().roomStates[roomId].contiguous, "1");
  socket.emit({ ...fixtures.subscribed, head_seq: "2" });
  await tick();
  assert.ok(h.calls[calls].path.includes("after_seq=1"));
});
test("automatic reconnect has six bounded attempts, and manual action resets the budget", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = harness();
  t.after(() => h.controller.dispose());
  await h.controller.start();
  for (let attempt = 0; attempt < 6; attempt += 1) {
    h.sockets.at(-1)!.close();
    t.mock.timers.tick(Math.min(500 * 2 ** attempt, 15_000) + 250);
  }
  assert.equal(h.sockets.length, 7);
  h.sockets.at(-1)!.close();
  assert.equal(h.controller.getSnapshot().connection, "stopped");
  h.controller.reconnect();
  assert.equal(h.sockets.length, 8);
  assert.equal(h.controller.getSnapshot().connection, "connecting");
});
test("failed automatic history recovery stops after three attempts until explicit sync", async (t) => {
  const h = harness(async ({ path }) => { if (path.includes("messages?")) throw new ApiError(503, "DATABASE_BUSY"); return undefined; });
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const socket = h.sockets[0]; socket.open(); socket.emit(fixtures.subscribed); await tick();
  for (let i = 0; i < 6; i += 1) { socket.emit(fixtures.heads); await tick(); }
  assert.equal(h.calls.filter((call) => call.path.includes("messages?")).length, 3);
  assert.match(h.controller.getSnapshot().roomStates[roomId].error ?? "", /자동 복구를 멈췄습니다/);
  h.controller.focus(); await tick();
  assert.equal(h.calls.filter((call) => call.path.includes("messages?")).length, 4);
});


test("reconnect restores missed history and overlapping live events exactly once", async (t) => {
  const gate = deferred<RequestResult>();
  let reconnecting = false;
  const h = harness(async ({ path }) => path.includes("messages?")
    ? reconnecting ? gate.promise : { body: history([message(1)]) } : undefined);
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const old = h.sockets[0]; old.open(); old.emit(fixtures.subscribed); await tick();
  old.close(); reconnecting = true;
  h.controller.reconnect();
  const next = h.sockets[1]; next.open(); next.emit({ ...fixtures.subscribed, head_seq: "3" });
  next.emit(created(message(3))); next.emit(created(message(3)));
  old.emit(created(message(4)));
  gate.resolve({ body: history([message(2), message(3)], "3", "3") }); await tick();
  const room = h.controller.getSnapshot().roomStates[roomId];
  assert.deepEqual(room.messages.map((item) => item.seq), ["1", "2", "3"]);
  assert.equal(new Set(room.messages.map((item) => item.message_id)).size, 3);
  assert.equal(room.contiguous, "3");
  assert.ok(h.calls.at(-1)?.path.includes("after_seq=1"));
});

test("confirmed session revocation clears private state and stops reconnect", async (t) => {
  const h = harness(); t.after(() => h.controller.dispose());
  await h.controller.start();
  h.sockets[0].open(); h.sockets[0].close(1008);
  assert.equal(h.controller.getSnapshot().actor, undefined);
  assert.deepEqual(h.controller.getSnapshot().roomStates, {});
  assert.equal(h.controller.getSnapshot().connection, "stopped");
});

test("successful subscriptions reset the budget across independent disconnects", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = harness(); t.after(() => h.controller.dispose());
  await h.controller.start();
  for (let index = 0; index < 8; index += 1) {
    const socket = h.sockets[index];
    assert.ok(socket);
    socket.open(); socket.emit({ ...fixtures.subscribed, head_seq: "0" }); await tick();
    socket.close(); t.mock.timers.tick(750); await tick();
  }
  assert.equal(h.sockets.length, 9);
  assert.notEqual(h.controller.getSnapshot().connection, "stopped");
});

test("a fresh subscription retries history after an earlier recovery failure budget", async (t) => {
  let broken = true;
  const h = harness(async ({ path }) => {
    if (!path.includes("messages?")) return;
    if (broken) throw new Error("synthetic history outage");
    return { body: history([message(1)]) };
  });
  t.after(() => h.controller.dispose());
  await h.controller.start();
  const socket = h.sockets[0]; socket.open(); socket.emit(fixtures.subscribed); await tick();
  for (let index = 0; index < 3; index += 1) { socket.emit(fixtures.heads); await tick(); }
  broken = false;
  h.controller.reconnect();
  h.sockets[1].open(); h.sockets[1].emit(fixtures.subscribed); await tick();
  assert.equal(h.controller.getSnapshot().roomStates[roomId].contiguous, "1");
});
