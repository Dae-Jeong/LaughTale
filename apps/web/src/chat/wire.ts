import Ajv from "ajv/dist/2020";
import addFormats from "ajv-formats";
import openapi from "../../../../contracts/chat/openapi.json";
import wsSchema from "../../../../contracts/chat/ws-server.schema.json";
import { sequence, type Actor, type Conversation, type Message } from "./model";
import type { components } from "./generated/http";
import type { WsServer } from "./generated/ws";

export type History = components["schemas"]["HistoryResponse"];
type WithRequiredFields<T> = T extends unknown ? Required<T> : never;
export type Frame = WithRequiredFields<WsServer>;

// OpenAPI annotations are not validation rules; data is never coerced or default-filled.
const ajv = new Ajv({ strict: false });
addFormats(ajv);
ajv.addSchema({ components: openapi.components }, "chat-http");
function httpValidator<T>(name: keyof components["schemas"]) {
  return ajv.compile<T>({ $ref: `chat-http#/components/schemas/${name}` });
}
const actorValid = httpValidator<components["schemas"]["Success_ActorData_"]>("Success_ActorData_");
const roomsValid = httpValidator<components["schemas"]["Success_list_ConversationData__"]>("Success_list_ConversationData__");
const storedValid = httpValidator<components["schemas"]["Success_StoredMessageData_"]>("Success_StoredMessageData_");
const historyValid = httpValidator<History>("HistoryResponse");
const frameValid = ajv.compile<WsServer>(wsSchema);

export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("서버 응답 형식을 확인해 주세요.");
  return value as Record<string, unknown>;
}
function invalid(): never { throw new Error("서버 응답이 채팅 계약과 일치하지 않습니다."); }
function normalizeMessage(message: Message): Message {
  const seq = sequence(message.seq);
  if (seq === "0" || !message.text.trim() || [...message.text].length > 2000 || !/(Z|\+00:00)$/.test(message.created_at)) invalid();
  return { ...message, seq };
}
export function parseActor(value: unknown): Actor {
  if (!actorValid(value)) invalid();
  return value.data;
}
export function parseRooms(value: unknown): Conversation[] {
  if (!roomsValid(value)) invalid();
  return value.data.map((room) => {
    if (room.kind !== "dm") invalid();
    return { ...room, head_seq: sequence(room.head_seq) };
  });
}
export function parseStored(value: unknown): Message {
  if (!storedValid(value) || value.data.state !== "stored") invalid();
  const { state, ...message } = value.data;
  void state;
  return normalizeMessage(message);
}
export function parseHistory(value: unknown): History {
  if (!historyValid(value)) invalid();
  return { data: value.data.map(normalizeMessage), meta: { ...value.meta,
    next_cursor: sequence(value.meta.next_cursor), snapshot_head_seq: sequence(value.meta.snapshot_head_seq) } };
}
export function parseFrame(value: unknown): Frame {
  if (!frameValid(value)) invalid();
  switch (value.type) {
    case "subscribed":
      if (value.protocol_version !== 1) invalid();
      return { ...value, type: value.type, protocol_version: value.protocol_version, head_seq: sequence(value.head_seq) };
    case "message.created":
      if (value.schema_version !== 1 || value.event_id !== value.message.message_id) invalid();
      return { ...value, type: value.type, schema_version: value.schema_version, message: normalizeMessage(value.message) };
    case "heads": return { ...value, type: value.type, items: value.items.map((item) => ({ ...item, head_seq: sequence(item.head_seq) })) };
    case "resync_required": return { ...value, type: value.type };
    case "error": return { ...value, type: value.type };
    default: return invalid();
  }
}
