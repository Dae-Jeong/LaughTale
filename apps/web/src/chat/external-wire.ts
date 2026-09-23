import Ajv from "ajv/dist/2020";
import addFormats from "ajv-formats";
import openapi from "../../../../contracts/chat/openapi.json";
import externalHeadSchema from "../../../../contracts/chat/external-ws-server.schema.json";
import { sequence } from "./model";
import type { components } from "./generated/http";

export type ExternalRoom = components["schemas"]["ExternalConversationData"];
export type ExternalMessage = components["schemas"]["ExternalMessageData"];
export type ExternalHistory = components["schemas"]["ExternalHistoryResponse"];
const ajv = new Ajv({ strict: false });
addFormats(ajv);
ajv.addSchema({ components: openapi.components }, "external-http");
const validators = new Map<string, ReturnType<typeof ajv.compile>>();
function validate<T>(name: string, value: unknown): T {
  let validator = validators.get(name);
  if (!validator) { validator = ajv.compile({ $ref: `external-http#/components/schemas/${name}` }); validators.set(name, validator); }
  if (!validator(value)) throw new Error("외부 채팅 응답이 계약과 일치하지 않습니다.");
  return value as T;
}
export const parseExternalRooms = (value: unknown) => validate<{ data: ExternalRoom[] }>("Success_list_ExternalConversationData__", value).data;
export const parseExternalMessage = (value: unknown) => validate<{ data: ExternalMessage }>("Success_ExternalMessageData_", value).data;
export const parseExternalHistory = (value: unknown) => validate<ExternalHistory>("ExternalHistoryResponse", value);
const headValidator = ajv.compile<{ type?: "head"; conversation_id: string; head_seq: string }>(externalHeadSchema);
export function parseExternalHead(value: unknown): { type?: "head"; conversation_id: string; head_seq: string } {
  if (!headValidator(value)) throw new Error("외부 채팅 구독 신호가 계약과 일치하지 않습니다.");
  sequence(value.head_seq);
  return value;
}

export function appendExternal(room: string, current: ExternalMessage[], page: ExternalMessage[]): ExternalMessage[] {
  const result = [...current];
  const ids = new Set(current.map((item) => item.message_id));
  const keyOf = (message: ExternalMessage): string => {
    if (message.sender_kind === "operator" && message.client_message_id) return JSON.stringify(["operator", message.sender_id, message.client_message_id]);
    if (message.sender_kind === "customer" && message.external_message_id) return JSON.stringify(["customer", message.external_message_id]);
    throw new Error("메시지 발신 종류와 업무 식별자가 일치하지 않습니다.");
  };
  // Same scopes as the DB constraints: room/operator/client key and room/provider id.
  // The kind prefix prevents equal string IDs in different directions colliding.
  const keys = new Set(current.map(keyOf));
  let seq = BigInt(current.at(-1)?.seq ?? "0");
  for (const message of page) {
    const key = keyOf(message);
    if (message.conversation_id !== room || BigInt(sequence(message.seq)) !== seq + 1n || ids.has(message.message_id) || keys.has(key)) {
      throw new Error("대화 내역의 순서 또는 식별자가 일치하지 않습니다.");
    }
    seq += 1n;
    ids.add(message.message_id);
    keys.add(key);
    result.push(message);
  }
  if (result.length > 5000) throw new Error("실험 화면의 5,000건 표시 한도입니다. 더 큰 내역은 테스트 결과로 확인해 주세요.");
  return result;
}
