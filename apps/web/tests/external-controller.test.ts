import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { ApiError, type RequestResult, type Transport } from "../src/chat/api";
import { ExternalChatController } from "../src/chat/external-controller";
import { parseExternalHead, type ExternalMessage, type ExternalRoom } from "../src/chat/external-wire";

const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const roomA = uuid(1), roomB = uuid(2), userA = uuid(3), userB = uuid(4);
const actor = (user = userA) => ({ data: { user_id: user, display_name: "synthetic" } });
const room = (id = roomA): ExternalRoom => ({ conversation_id: id, connection_id: uuid(5), external_conversation_id: "mock-room", title: "mock", profile: "telegram", head_seq: "0" });
const message = (changes: Partial<ExternalMessage> = {}): ExternalMessage => ({
  message_id: uuid(10), conversation_id: roomA, sender_kind: "operator", sender_id: userA,
  client_message_id: uuid(20), external_message_id: null, seq: "1", text: "hello",
  occurred_at: "2026-09-08T00:00:00Z", received_at: "2026-09-08T00:00:00Z", created_at: "2026-09-08T00:00:00Z",
  operation_id: uuid(30), delivery_state: "accepted", effect_id: uuid(40), external_sender_id: null, ...changes,
});
const history = (data: ExternalMessage[] = [], cursor = data.at(-1)?.seq ?? "0", head = cursor, more = false) => ({ data, meta: { next_cursor: cursor, snapshot_head_seq: head, has_more: more } });
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (value: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
class Socket {
  readyState = 0;
  onopen?: () => void;
  onmessage?: (event: { data: string }) => void;
  onclose?: () => void;
  onerror?: () => void;
  sent: unknown[] = [];
  send(data: string) { this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; this.onclose?.(); }
  open() { this.readyState = 1; this.onopen?.(); }
  emit(head = "0", id = roomA) { this.onmessage?.({ data: JSON.stringify({ type: "head", conversation_id: id, head_seq: head }) }); }
}
type Handler = (path: string, options: Parameters<Transport["request"]>[1]) => Promise<RequestResult | undefined>;
function harness(handler?: Handler) {
  const sockets: Socket[] = [];
  const calls: { path: string; signal: AbortSignal; body?: unknown }[] = [];
  let key = 20;
  const transport: Transport = {
    async request(path, options) {
      calls.push({ path, ...options });
      const override = await handler?.(path, options);
      if (override) return override;
      if (path === "/v1/session" || path === "/v1/dev/session") return { body: actor() };
      if (path === "/v1/external-conversations") return { body: { data: [room(), room(roomB)] } };
      if (path.includes("/messages?")) return { body: history() };
      throw new Error(`Unhandled test request ${path}`);
    },
    socket() { const socket = new Socket(); sockets.push(socket); return socket as unknown as WebSocket; },
  };
  return { controller: new ExternalChatController(transport, () => uuid(key++)), sockets, calls };
}

test("external same-room selection preserves socket and recovery lifecycle", async (t) => {
  const { controller, sockets } = harness(); t.after(() => controller.dispose());
  await controller.start(); await setImmediate();
  sockets[0].open(); sockets[0].emit(); await setImmediate();
  controller.selectRoom(roomA);
  assert.equal(sockets.length, 1);
  sockets[0].emit(); await setImmediate();
  assert.equal(controller.getSnapshot().connected, true);
  assert.deepEqual(sockets[0].sent, [{ type: "subscribe", conversation_id: roomA }]);
});

test("external late ACK updates originating room without clearing another room pending", async (t) => {
  const ack = deferred<RequestResult>();
  const { controller } = harness(async (path, options) => options.body && path.includes("/messages") ? ack.promise : undefined);
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  const send = controller.send("hello");
  controller.selectRoom(roomB);
  assert.equal(controller.getSnapshot().roomStates[roomA].pending?.state, "sending");
  ack.resolve({ body: { data: message() } }); await send; await setImmediate();
  assert.equal(controller.getSnapshot().selected, roomB);
  assert.equal(controller.getSnapshot().roomStates[roomA].pending, undefined);
  assert.deepEqual(controller.getSnapshot().roomStates[roomB].messages, []);
});

test("external definite HTTP rejection is distinct from unknown storage", async (t) => {
  let status = 422;
  const { controller } = harness(async (path, options) => { if (options.body && path.includes("/messages")) throw new ApiError(status, "INVALID_INPUT"); return undefined; });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  await controller.send("hello"); assert.equal(controller.getSnapshot().roomStates[roomA].pending?.state, "rejected");
  controller.dismissRejected(); status = 503;
  await controller.send("hello"); assert.equal(controller.getSnapshot().roomStates[roomA].pending?.state, "unknown");
});

test("external unknown retry retains original id and body", async (t) => {
  const { controller, calls } = harness(async (path, options) => { if (options.body && path.includes("/messages")) throw new TypeError("offline"); return undefined; });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  await controller.send("hello"); await controller.send("ignored", true);
  const writes = calls.filter((call) => call.path.includes("/messages") && call.body);
  assert.equal(writes.length, 2); assert.deepEqual(writes[0].body, writes[1].body);
});

test("external history proof wins over a late HTTP failure", async (t) => {
  const ack = deferred<RequestResult>(); let stored = false;
  const { controller, sockets } = harness(async (path, options) => {
    if (options.body && path.includes("/messages")) return ack.promise;
    if (stored && path.includes("/messages?")) { const after = new URL(path, "http://test").searchParams.get("after_seq"); return { body: history(after === "0" ? [message()] : [], "1") }; }
  });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  const write = controller.send("hello"); stored = true; sockets[0].open(); sockets[0].emit("1"); await setImmediate();
  assert.equal(controller.getSnapshot().roomStates[roomA].pending, undefined);
  ack.reject(new TypeError("lost response")); await write;
  assert.equal(controller.getSnapshot().roomStates[roomA].pending, undefined);
  assert.equal(controller.getSnapshot().roomStates[roomA].messages.length, 1);
});

test("external session cookies are issued serially and stale actor is fenced", async (t) => {
  const first = deferred<RequestResult>(); const users: string[] = [];
  const { controller } = harness(async (path, options) => {
    if (path === "/v1/dev/session") {
      const user = (options.body as { user: string }).user; users.push(user);
      return user === "user_a" ? first.promise : { body: actor(userB) };
    }
  });
  t.after(() => controller.dispose());
  const a = controller.switchUser("user_a"); await setImmediate();
  const b = controller.switchUser("user_b"); await setImmediate();
  assert.deepEqual(users, ["user_a"]);
  first.resolve({ body: actor(userA) }); await Promise.all([a, b]);
  assert.deepEqual(users, ["user_a", "user_b"]);
  assert.equal(controller.getSnapshot().actor?.user_id, userB);
});

test("external dispose aborts send and ignores its late result", async () => {
  const ack = deferred<RequestResult>();
  const { controller, calls } = harness(async (path, options) => options.body && path.includes("/messages") ? ack.promise : undefined);
  await controller.start(); await setImmediate(); const sending = controller.send("hello");
  controller.dispose(); assert.equal(calls.at(-1)?.signal.aborted, true);
  ack.resolve({ body: { data: message() } }); await sending;
  assert.equal(controller.getSnapshot().actor, undefined); assert.deepEqual(controller.getSnapshot().roomStates, {});
});

test("external impossible history gap fails and automatic recovery stops after three failures", async (t) => {
  let reads = 0;
  const { controller, sockets } = harness(async (path) => { if (path.includes("/messages?")) { reads++; return { body: history([], "0", "1", false) }; } });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  sockets[0].open();
  for (let i = 0; i < 5; i++) { sockets[0].emit("1"); await setImmediate(); }
  assert.equal(reads, 3); assert.equal(controller.getSnapshot().roomStates[roomA].messages.length, 0);
  assert.match(controller.getSnapshot().roomStates[roomA].error ?? "", /누락/);
  controller.recover(); await setImmediate(); assert.equal(reads, 4);
});

test("external stale socket from previous room cannot update selected room", async (t) => {
  const { controller, sockets } = harness(); t.after(() => controller.dispose());
  await controller.start(); await setImmediate(); controller.selectRoom(roomB);
  sockets[0].emit("100");
  assert.equal(controller.getSnapshot().roomStates[roomA].head, "0");
  assert.equal(controller.getSnapshot().selected, roomB);
});

test("external subscription rejects other room and over-range head", async (t) => {
  assert.throws(() => parseExternalHead({ type: "head", conversation_id: roomA, head_seq: "9223372036854775808" }));
  assert.throws(() => parseExternalHead({ type: "head", conversation_id: roomA, head_seq: 1 }));
  const { controller, sockets } = harness(); t.after(() => controller.dispose());
  await controller.start(); await setImmediate(); sockets[0].open(); sockets[0].emit("0", roomB);
  assert.equal(sockets[0].readyState, 3); assert.equal(controller.getSnapshot().connected, false);
});

test("external effect id can appear with accepted delivery without mutating immutable message", async (t) => {
  const pending = message({ effect_id: null, delivery_state: "pending" });
  const { controller } = harness(async (path) => {
    if (path.includes("/messages?")) return { body: history([pending]) };
    if (path.endsWith(`/messages/${pending.message_id}`)) return { body: { data: message() } };
  });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  assert.equal(controller.getSnapshot().roomStates[roomA].messages[0].delivery_state, "accepted");
  assert.equal(controller.getSnapshot().roomStates[roomA].messages[0].effect_id, uuid(40));
});

test("external reconnect budget is finite and manual recovery resets it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, sockets } = harness(); t.after(() => controller.dispose());
  await controller.start(); await setImmediate();
  for (let i = 0; i < 7; i++) {
    sockets.at(-1)?.close(); t.mock.timers.tick(15000); await setImmediate();
  }
  assert.equal(sockets.length, 7);
  controller.recover(); await setImmediate(); assert.equal(sockets.length, 8);
});

test("external older HTTP ACK cannot erase a newer pending request", async (t) => {
  const ack1 = deferred<RequestResult>(), ack2 = deferred<RequestResult>(); let stored = false; let sends = 0;
  const { controller, sockets } = harness(async (path, options) => {
    if (options.body && path.includes("/messages")) return ++sends === 1 ? ack1.promise : ack2.promise;
    if (stored && path.includes("/messages?")) { const after = new URL(path, "http://test").searchParams.get("after_seq"); return { body: history(after === "0" ? [message()] : [], "1") }; }
  });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  const first = controller.send("hello"); stored = true; sockets[0].open(); sockets[0].emit("1"); await setImmediate();
  const second = controller.send("next");
  ack1.resolve({ body: { data: message() } }); await first;
  assert.equal(controller.getSnapshot().roomStates[roomA].pending?.id, uuid(21));
  ack2.reject(new TypeError("offline")); await second;
  assert.equal(controller.getSnapshot().roomStates[roomA].pending?.text, "next");
});

test("external more than 100 unsettled operations progress in bounded batches", async (t) => {
  const items = Array.from({ length: 101 }, (_, index) => message({ message_id: uuid(1000 + index), client_message_id: uuid(2000 + index), operation_id: uuid(3000 + index), seq: String(index + 1), delivery_state: "pending", effect_id: null }));
  let statusReads = 0;
  const { controller } = harness(async (path) => {
    if (path.includes("/messages?")) { const after = Number(new URL(path, "http://test").searchParams.get("after_seq")); const page = items.slice(after, after + 100); const cursor = after + page.length; return { body: history(page, String(cursor), "101", cursor < 101) }; }
    const item = items.find((entry) => path.endsWith(`/messages/${entry.message_id}`));
    if (item) { statusReads++; return { body: { data: { ...item, delivery_state: "accepted", effect_id: uuid(4000 + Number(item.seq)) } } }; }
  });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  assert.equal(statusReads, 20);
  for (let i = 0; i < 5; i++) { controller.recover(); await setImmediate(); }
  assert.equal(statusReads, 101);
  assert.equal(controller.getSnapshot().roomStates[roomA].messages.filter((item) => item.delivery_state === "accepted").length, 101);
});

test("external old unknown operations do not monopolize status polling", async (t) => {
  const items = Array.from({ length: 21 }, (_, index) => message({ message_id: uuid(1000 + index), client_message_id: uuid(2000 + index), operation_id: uuid(3000 + index), seq: String(index + 1), delivery_state: index < 20 ? "unknown" : "pending", effect_id: null }));
  const { controller } = harness(async (path) => {
    if (path.includes("/messages?")) { const after = Number(new URL(path, "http://test").searchParams.get("after_seq")); return { body: history(items.slice(after), "21") }; }
    const item = items.find((entry) => path.endsWith(`/messages/${entry.message_id}`));
    if (item) return { body: { data: item.seq === "21" ? { ...item, delivery_state: "accepted", effect_id: uuid(5000) } : item } };
  });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate(); controller.recover(); await setImmediate();
  assert.equal(controller.getSnapshot().roomStates[roomA].messages.at(-1)?.delivery_state, "accepted");
});

test("external completed status updates survive a later request failure", async (t) => {
  const first = message({ delivery_state: "pending", effect_id: null });
  const second = message({ message_id: uuid(11), client_message_id: uuid(21), operation_id: uuid(31), seq: "2", delivery_state: "pending", effect_id: null });
  const { controller } = harness(async (path) => {
    if (path.includes("/messages?")) return { body: history([first, second]) };
    if (path.endsWith(`/messages/${first.message_id}`)) return { body: { data: message() } };
    if (path.endsWith(`/messages/${second.message_id}`)) throw new TypeError("synthetic timeout");
  });
  t.after(() => controller.dispose()); await controller.start(); await setImmediate();
  assert.equal(controller.getSnapshot().roomStates[roomA].messages[0].delivery_state, "accepted");
  assert.match(controller.getSnapshot().roomStates[roomA].error ?? "", /timeout/);
});
