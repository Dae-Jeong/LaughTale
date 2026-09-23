import type { components } from "./generated/http";
export type Message = components["schemas"]["HistoryResponse"]["data"][number];
export type Actor = components["schemas"]["ActorData"];
export type Conversation = components["schemas"]["ConversationData"];
export type Delivery = "sending" | "stored" | "unknown" | "rejected";
export type Outgoing = {
  client_message_id: string;
  conversation_id: string;
  sender_id: string;
  text: string;
  state: Delivery;
  error?: string;
};
export type RoomState = {
  conversationId: string;
  messages: Message[];
  buffer: Record<string, Message>;
  outgoing: Outgoing[];
  contiguous: string;
  head: string;
  syncing: boolean;
  error?: string;
};

export function sequence(value: unknown): string {
  if (typeof value !== "string" || !/^(0|[1-9][0-9]{0,18})$/.test(value)) throw new Error("올바르지 않은 순서 값입니다.");
  const parsed = BigInt(value);
  if (parsed > 9223372036854775807n) throw new Error("순서 값이 허용 범위를 넘었습니다.");
  return parsed.toString();
}

export const maxSequence = (a: string, b: string): string => BigInt(a) > BigInt(b) ? a : b;
export const emptyRoom = (conversationId: string): RoomState => ({ conversationId, messages: [], buffer: {}, outgoing: [], contiguous: "0", head: "0", syncing: false });
export const needsRecovery = (room: RoomState): boolean => BigInt(room.head) > BigInt(room.contiguous);

function sameMessage(a: Message, b: Message): boolean {
  return a.message_id === b.message_id && a.conversation_id === b.conversation_id &&
    a.sender_id === b.sender_id && a.client_message_id === b.client_message_id &&
    a.seq === b.seq && a.text === b.text && a.created_at === b.created_at;
}

export function mergeMessages(room: RoomState, incoming: Message[]): RoomState {
  const buffer = { ...room.buffer };
  const messages = [...room.messages];
  const known = [...room.messages, ...Object.values(room.buffer)];
  const bySeq = new Map(known.map((item) => [item.seq, item]));
  const byId = new Map(known.map((item) => [item.message_id, item]));
  const keyOf = (item: Message) => `${item.sender_id}:${item.client_message_id}`;
  const byKey = new Map(known.map((item) => [keyOf(item), item]));
  let head = room.head;
  let contiguous = room.contiguous;
  for (const raw of incoming) {
    const message = { ...raw, seq: sequence(raw.seq) };
    if (message.conversation_id !== room.conversationId) throw new Error("다른 대화의 메시지를 병합할 수 없습니다.");
    if (message.seq === "0") throw new Error("메시지 순서는 1부터 시작해야 합니다.");
    const collisions = [bySeq.get(message.seq), byId.get(message.message_id), byKey.get(keyOf(message))];
    if (collisions.some((existing) => existing && !sameMessage(existing, message))) {
      throw new Error("동일한 메시지의 저장 결과가 일치하지 않습니다.");
    }
    bySeq.set(message.seq, message);
    byId.set(message.message_id, message);
    byKey.set(keyOf(message), message);
    head = maxSequence(head, message.seq);
    if (BigInt(message.seq) > BigInt(contiguous)) buffer[message.seq] = message;
    // 큰 seq를 먼저 받아도 빈 번호를 넘어가지 않습니다.
    while (buffer[(BigInt(contiguous) + 1n).toString()]) {
      contiguous = (BigInt(contiguous) + 1n).toString();
      messages.push(buffer[contiguous]);
      delete buffer[contiguous];
    }
  }
  // 유실 구간은 권위 있는 history로 복구합니다. 이벤트 큐는 무한히 늘리지 않습니다.
  const boundedBuffer = Object.fromEntries(Object.entries(buffer)
    .sort(([a], [b]) => BigInt(a) < BigInt(b) ? -1 : 1).slice(0, 500));
  const incomingByKey = new Map(incoming.map((item) => [keyOf(item), item]));
  const outgoing = room.outgoing.map((draft): Outgoing => {
    const stored = incomingByKey.get(`${draft.sender_id}:${draft.client_message_id}`);
    if (!stored) return draft;
    if (stored.text !== draft.text || stored.conversation_id !== draft.conversation_id) throw new Error("전송 결과의 본문이 일치하지 않습니다.");
    return { ...draft, state: "stored", error: undefined };
  });
  return { ...room, messages, buffer: boundedBuffer, outgoing, head, contiguous };
}

export function markDelivery(room: RoomState, key: string, state: Delivery, error?: string): RoomState {
  return { ...room, outgoing: room.outgoing.map((draft) => draft.client_message_id !== key || draft.state === "stored"
    ? draft : { ...draft, state, error }) };
}

export function retryDraft(room: RoomState, key: string): Outgoing {
  const draft = room.outgoing.find((item) => item.client_message_id === key);
  if (!draft || draft.state !== "unknown") throw new Error("결과 확인이 필요한 메시지만 재시도할 수 있습니다.");
  return { ...draft, state: "sending", error: undefined };
}
