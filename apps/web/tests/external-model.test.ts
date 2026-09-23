import assert from "node:assert/strict";
import test from "node:test";
import { appendExternal, type ExternalMessage } from "../src/chat/external-wire";

const message = (seq = "1", id = "message-1"): ExternalMessage => ({
  message_id: id, conversation_id: "room-1", sender_kind: "customer", sender_id: "customer-1",
  client_message_id: null, external_message_id: `provider-${id}`, seq, text: "합성 테스트",
  occurred_at: "2026-09-08T00:00:00Z", received_at: "2026-09-08T00:00:00Z", created_at: "2026-09-08T00:00:00Z",
  operation_id: null, delivery_state: null, effect_id: null, external_sender_id: "sender",
});
test("external history appends only contiguous messages", () => {
  assert.deepEqual(appendExternal("room-1", [message()], [message("2", "message-2")]).map((item) => item.seq), ["1", "2"]);
});
test("external history rejects gaps, wrong rooms and duplicate identities", () => {
  assert.throws(() => appendExternal("room-1", [], [message("2")]));
  assert.throws(() => appendExternal("room-2", [], [message()]));
  assert.throws(() => appendExternal("room-1", [message()], [message("2")]));
});
test("external sequence is lossless above Number.MAX_SAFE_INTEGER", () => {
  assert.equal(appendExternal("room-1", [message("9007199254740992")], [message("9007199254740993", "message-2")]).at(-1)?.seq, "9007199254740993");
});
test("external view refuses unbounded growth", () => {
  const existing = Array.from({ length: 5000 }, (_, index) => message(String(index + 1), `message-${index}`));
  assert.throws(() => appendExternal("room-1", existing, [message("5001", "overflow")]));
});
test("external business keys reject conflicting new message IDs and text", () => {
  const original = { ...message(), sender_kind: "operator" as const, client_message_id: "same-key", external_message_id: null };
  assert.throws(() => appendExternal("room-1", [original], [{ ...original, seq: "2", message_id: "new-id", text: "changed" }]));
  const customer = message();
  assert.throws(() => appendExternal("room-1", [customer], [{ ...customer, seq: "2", message_id: "new-id", sender_id: "other", text: "changed" }]));
});
test("external business key scope separates operators and message directions", () => {
  const original = { ...message(), sender_kind: "operator" as const, client_message_id: "shared", external_message_id: null };
  const other = { ...original, seq: "2", message_id: "second", sender_id: "other" };
  const customer = { ...message("3", "third"), external_message_id: "shared" };
  assert.equal(appendExternal("room-1", [original], [other, customer]).length, 3);
});
