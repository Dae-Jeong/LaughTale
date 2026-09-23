// Generated from contracts/chat/ws-server.schema.json. Do not edit.

export type WsServer = Subscribed | MessageCreated | Heads | ResyncRequired | WSError;
export type ConversationId = string;
export type HeadSeq = string;
export type ProtocolVersion = 1;
export type Type = "subscribed";
export type EventId = string;
export type ClientMessageId = string;
export type ConversationId1 = string;
export type CreatedAt = string;
export type MessageId = string;
export type SenderId = string;
export type Seq = string;
export type Text = string;
export type SchemaVersion = 1;
export type Type1 = "message.created";
export type ConversationId2 = string;
export type HeadSeq1 = string;
export type Items = HeadItem[];
export type Type2 = "heads";
export type ConversationId3 = string;
export type ResyncReason = "queue_overflow" | "recovery_unavailable";
export type Type3 = "resync_required";
export type Code =
  "UNAUTHENTICATED" | "CONVERSATION_NOT_FOUND" | "INVALID_INPUT" | "HTTP_ERROR" | "DATABASE_BUSY" | "INTERNAL_ERROR";
export type Type4 = "error";

export interface Subscribed {
  conversation_id: ConversationId;
  head_seq: HeadSeq;
  protocol_version?: ProtocolVersion;
  type?: Type;
}
export interface MessageCreated {
  event_id: EventId;
  message: MessageData;
  schema_version?: SchemaVersion;
  type?: Type1;
}
export interface MessageData {
  client_message_id: ClientMessageId;
  conversation_id: ConversationId1;
  created_at: CreatedAt;
  message_id: MessageId;
  sender_id: SenderId;
  seq: Seq;
  text: Text;
}
export interface Heads {
  items: Items;
  type?: Type2;
}
export interface HeadItem {
  conversation_id: ConversationId2;
  head_seq: HeadSeq1;
}
export interface ResyncRequired {
  conversation_id: ConversationId3;
  reason: ResyncReason;
  type?: Type3;
}
export interface WSError {
  code: Code;
  type?: Type4;
}
