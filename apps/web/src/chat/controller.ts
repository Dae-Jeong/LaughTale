import { ApiError, type Transport } from "./api";
import { emptyRoom, markDelivery, maxSequence, mergeMessages, needsRecovery, retryDraft,
  type Actor, type Conversation, type Outgoing, type RoomState } from "./model";
import { parseActor, parseFrame, parseHistory, parseRooms, parseStored, type Frame } from "./wire";

export type ChatState = {
  actor?: Actor;
  rooms: Conversation[];
  roomStates: Record<string, RoomState>;
  selected?: string;
  sessionBusy: boolean;
  connection: "offline" | "connecting" | "connected" | "reconnecting" | "stopped";
  error?: string;
  requestId?: string;
};
export const initialState: ChatState = { rooms: [], roomStates: {}, sessionBusy: false, connection: "offline" };
const messageOf = (error: unknown): string => error instanceof Error ? error.message : "요청을 처리하지 못했습니다.";

export class ChatController {
  private state: ChatState = initialState;
  private listeners = new Set<() => void>();
  private epoch = 0;
  private requests = new Set<AbortController>();
  private ws?: WebSocket;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private handshakeTimer?: ReturnType<typeof setTimeout>;
  private reconnectAttempt = 0;
  private subscribed = new Set<string>();
  private syncs = new Map<string, Promise<void>>();
  private recoveryFailures = new Map<string, number>();
  private sessionQueue: Promise<void> = Promise.resolve();

  constructor(private transport: Transport, private randomId = () => crypto.randomUUID()) {}
  getSnapshot = (): ChatState => this.state;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private update(change: Partial<ChatState>): void {
    this.state = { ...this.state, ...change };
    this.listeners.forEach((listener) => listener());
  }
  private changeRoom(id: string, change: (room: RoomState) => RoomState): void {
    if (!this.state.roomStates[id]) return;
    this.update({ roomStates: { ...this.state.roomStates, [id]: change(this.state.roomStates[id]) } });
  }
  private stopConnection(): void {
    clearTimeout(this.reconnectTimer);
    clearTimeout(this.handshakeTimer);
    const socket = this.ws;
    this.ws = undefined;
    this.subscribed.clear();
    socket?.close();
  }
  private invalidate(): number {
    this.epoch += 1;
    this.stopConnection();
    this.requests.forEach((request) => request.abort());
    this.requests.clear();
    this.syncs.clear();
    this.recoveryFailures.clear();
    this.reconnectAttempt = 0;
    this.state = initialState;
    return this.epoch;
  }
  dispose(): void { this.invalidate(); }

  private async request(path: string, epoch: number, body?: unknown): Promise<unknown> {
    const abort = new AbortController();
    this.requests.add(abort);
    const timeout = setTimeout(() => abort.abort(), 10_000);
    try {
      const result = await this.transport.request(path, { body, signal: abort.signal });
      if (epoch !== this.epoch) throw new Error("이전 사용자 요청입니다.");
      this.update({ requestId: result.requestId });
      return result.body;
    } catch (error) {
      if (epoch === this.epoch && error instanceof ApiError) this.update({ requestId: error.requestId });
      if (error instanceof Error && (error.name === "AbortError" || error.name === "TypeError")) {
        throw new Error("서버에 연결하지 못했거나 응답 시간이 초과되었습니다. 연결을 확인해 주세요.");
      }
      throw error;
    } finally {
      clearTimeout(timeout);
      this.requests.delete(abort);
    }
  }

  async start(): Promise<void> {
    const epoch = this.epoch;
    this.update({ sessionBusy: true });
    try {
      const actor = parseActor(await this.request("/v1/session", epoch));
      if (epoch === this.epoch) await this.loadActor(actor, epoch);
    } catch (error) {
      if (epoch === this.epoch && !(error instanceof ApiError && error.status === 401)) this.update({ error: messageOf(error) });
    } finally { if (epoch === this.epoch) this.update({ sessionBusy: false }); }
  }

  switchUser(user: "user_a" | "user_b"): Promise<void> {
    const epoch = this.invalidate();
    this.update({ sessionBusy: true });
    // 쿠키 발급 요청은 직렬화해 이전 사용자의 늦은 Set-Cookie가 새 세션을 덮지 않게 합니다.
    const next = this.sessionQueue.then(async () => {
      if (epoch !== this.epoch) return;
      try {
        const actor = parseActor(await this.request("/v1/dev/session", epoch, { user }));
        if (epoch === this.epoch) await this.loadActor(actor, epoch);
      } catch (error) { if (epoch === this.epoch) this.update({ error: messageOf(error) }); }
      finally { if (epoch === this.epoch) this.update({ sessionBusy: false }); }
    });
    this.sessionQueue = next;
    return next;
  }
  private async loadActor(actor: Actor, epoch: number): Promise<void> {
    const rooms = parseRooms(await this.request("/v1/internal-conversations", epoch));
    if (epoch !== this.epoch) return;
    this.update({ actor, rooms, selected: rooms[0]?.conversation_id,
      roomStates: Object.fromEntries(rooms.map((room) => [room.conversation_id, { ...emptyRoom(room.conversation_id), head: room.head_seq }])), error: undefined });
    this.connect(epoch);
  }
  selectRoom(id: string): void {
    if (!this.state.roomStates[id] || this.state.selected === id) return;
    if (this.ws?.readyState === 1 && this.state.selected) this.ws.send(JSON.stringify({ type: "unsubscribe", conversation_id: this.state.selected }));
    this.subscribed.clear();
    this.update({ selected: id, error: undefined });
    this.subscribeSelected();
  }
  private subscribeSelected(): void {
    if (this.ws?.readyState !== 1 || !this.state.selected) return;
    this.ws.send(JSON.stringify({ type: "subscribe", conversation_id: this.state.selected }));
    clearTimeout(this.handshakeTimer);
    this.handshakeTimer = setTimeout(() => this.ws?.close(), 10_000);
  }
  private connect(epoch: number): void {
    if (epoch !== this.epoch || !this.state.actor) return;
    this.update({ connection: this.reconnectAttempt ? "reconnecting" : "connecting" });
    let socket: WebSocket;
    try { socket = this.transport.socket(); }
    catch (error) { this.update({ error: messageOf(error) }); this.scheduleReconnect(epoch); return; }
    this.ws = socket;
    const current = () => epoch === this.epoch && this.ws === socket;
    this.handshakeTimer = setTimeout(() => { if (current()) socket.close(); }, 10_000);
    socket.onopen = () => {
      if (current()) {
        clearTimeout(this.handshakeTimer);
        this.update({ connection: "connected" });
        this.subscribeSelected();
      }
    };
    socket.onmessage = (event) => {
      if (!current()) return;
      try { this.receive(parseFrame(JSON.parse(event.data)), epoch); }
      catch (error) { this.update({ error: messageOf(error) }); socket.close(); }
    };
    socket.onerror = () => { if (current()) this.update({ error: "실시간 연결을 확인해 주세요." }); };
    socket.onclose = (event) => {
      if (!current()) return;
      if (event.code === 1008) {
        this.invalidate();
        this.update({ connection: "stopped", error: new ApiError(401, "UNAUTHENTICATED").message });
        return;
      }
      clearTimeout(this.handshakeTimer);
      this.ws = undefined;
      this.subscribed.clear();
      this.scheduleReconnect(epoch);
    };
  }
  private scheduleReconnect(epoch: number): void {
    if (this.reconnectAttempt >= 6) {
      this.update({ connection: "stopped", error: "자동 재연결을 멈췄습니다. 연결 재시도를 눌러 주세요." });
      return;
    }
    const delay = Math.min(500 * 2 ** this.reconnectAttempt++, 15_000) + Math.floor(Math.random() * 250);
    this.update({ connection: "reconnecting" });
    this.reconnectTimer = setTimeout(() => this.connect(epoch), delay);
  }
  reconnect = (): void => {
    this.stopConnection();
    this.reconnectAttempt = 0;
    this.connect(this.epoch);
  };
  focus = (): void => {
    if (!this.state.actor) return;
    if (this.ws?.readyState !== 1) { this.reconnect(); return; }
    const id = this.state.selected;
    if (id && this.subscribed.has(id)) void this.sync(id, this.epoch, true);
  };
  private receive(frame: Frame, epoch: number): void {
    if (frame.type === "error") {
      this.update({ error: new ApiError(400, frame.code).message });
      return;
    }
    if (frame.type === "message.created") {
      const id = frame.message.conversation_id;
      this.changeRoom(id, (room) => mergeMessages(room, [frame.message]));
      if (this.subscribed.has(id) && needsRecovery(this.state.roomStates[id])) void this.sync(id, epoch);
      return;
    }
    if (frame.type === "subscribed") {
      if (frame.conversation_id !== this.state.selected) return;
      clearTimeout(this.handshakeTimer);
      this.subscribed.add(frame.conversation_id);
      this.reconnectAttempt = 0;
      this.recoveryFailures.delete(frame.conversation_id);
      this.update({ error: undefined });
      this.changeRoom(frame.conversation_id, (room) => ({ ...room, head: maxSequence(room.head, frame.head_seq) }));
      void this.sync(frame.conversation_id, epoch);
      return;
    }
    if (frame.type === "heads") {
      for (const item of frame.items) {
        this.changeRoom(item.conversation_id, (room) => ({ ...room, head: maxSequence(room.head, item.head_seq) }));
        if (this.subscribed.has(item.conversation_id) && needsRecovery(this.state.roomStates[item.conversation_id])) void this.sync(item.conversation_id, epoch);
      }
      return;
    }
    if (this.subscribed.has(frame.conversation_id)) void this.sync(frame.conversation_id, epoch, true);
  }

  private sync(id: string, epoch: number, discoverHead = false): Promise<void> {
    const existing = this.syncs.get(id);
    if (existing) return existing;
    if (discoverHead) this.recoveryFailures.delete(id);
    else if ((this.recoveryFailures.get(id) ?? 0) >= 3) return Promise.resolve();
    const work = this.recover(id, epoch, discoverHead);
    this.syncs.set(id, work);
    void work.finally(() => { if (this.syncs.get(id) === work) this.syncs.delete(id); });
    return work;
  }
  private async recover(id: string, epoch: number, discoverHead: boolean): Promise<void> {
    this.changeRoom(id, (room) => ({ ...room, syncing: true, error: undefined }));
    try {
      let cursor = this.state.roomStates[id].contiguous;
      let snapshot: string | undefined = discoverHead ? undefined : this.state.roomStates[id].head;
      for (let page = 0; page < 100; page += 1) {
        if (epoch !== this.epoch || !this.subscribed.has(id)) return;
        const query = new URLSearchParams({ after_seq: cursor, limit: "100" });
        if (snapshot !== undefined) query.set("snapshot_head_seq", snapshot);
        const history = parseHistory(await this.request(`/v1/internal-conversations/${id}/messages?${query}`, epoch));
        if (epoch !== this.epoch) return;
        if (snapshot !== undefined && snapshot !== history.meta.snapshot_head_seq) throw new Error("내역 조회의 기준 순서가 바뀌었습니다.");
        snapshot = history.meta.snapshot_head_seq;
        let expected = BigInt(cursor);
        for (const message of history.data) {
          expected += 1n;
          if (message.conversation_id !== id || BigInt(message.seq) !== expected || expected > BigInt(snapshot)) throw new Error("내역에 누락되거나 잘못된 순서가 있습니다.");
        }
        if (history.meta.next_cursor !== expected.toString() || history.meta.has_more !== (expected < BigInt(snapshot)) ||
          (history.meta.has_more && history.data.length === 0)) throw new Error("내역 페이지의 연속성을 확인하지 못했습니다.");
        this.changeRoom(id, (room) => ({ ...mergeMessages(room, history.data), head: maxSequence(room.head, snapshot!) }));
        cursor = history.meta.next_cursor;
        if (history.meta.has_more) continue;
        const room = this.state.roomStates[id];
        if (!needsRecovery(room)) { this.recoveryFailures.delete(id); return; }
        cursor = room.contiguous;
        snapshot = room.head;
      }
      throw new Error("한 번에 조회할 수 있는 내역을 넘었습니다. 내역 동기화를 다시 눌러 주세요.");
    } catch (error) {
      if (epoch === this.epoch) {
        const failures = (this.recoveryFailures.get(id) ?? 0) + 1;
        this.recoveryFailures.set(id, failures);
        this.changeRoom(id, (room) => ({ ...room, error: failures >= 3
          ? `${messageOf(error)} 자동 복구를 멈췄습니다. 내역 동기화를 눌러 주세요.` : messageOf(error) }));
      }
    }
    finally { if (epoch === this.epoch) this.changeRoom(id, (room) => ({ ...room, syncing: false })); }
  }

  async send(text: string): Promise<void> {
    const actor = this.state.actor;
    const id = this.state.selected;
    if (!actor || !id || !text.trim() || [...text].length > 2000) return;
    const room = this.state.roomStates[id];
    if (room.outgoing.filter((item) => item.state !== "stored").length >= 100) {
      this.update({ error: "결과 확인이 필요한 메시지를 먼저 확인해 주세요." }); return;
    }
    const draft: Outgoing = { conversation_id: id, sender_id: actor.user_id, client_message_id: this.randomId(), text, state: "sending" };
    this.changeRoom(id, (state) => ({ ...state, outgoing: [...state.outgoing, draft] }));
    await this.transmit(draft, this.epoch);
  }
  async retry(key: string): Promise<void> {
    const id = this.state.selected;
    if (!id) return;
    const draft = retryDraft(this.state.roomStates[id], key);
    this.changeRoom(id, (room) => markDelivery(room, key, "sending"));
    await this.transmit(draft, this.epoch);
  }
  private async transmit(draft: Outgoing, epoch: number): Promise<void> {
    const id = draft.conversation_id;
    try {
      const stored = parseStored(await this.request(`/v1/internal-conversations/${id}/messages`, epoch,
        { client_message_id: draft.client_message_id, text: draft.text }));
      if (epoch !== this.epoch) return;
      if (stored.conversation_id !== id || stored.sender_id !== draft.sender_id || stored.client_message_id !== draft.client_message_id || stored.text !== draft.text) throw new Error("저장 응답이 전송한 메시지와 일치하지 않습니다.");
      this.changeRoom(id, (room) => mergeMessages(room, [stored]));
      if (this.subscribed.has(id) && needsRecovery(this.state.roomStates[id])) void this.sync(id, epoch);
    } catch (error) {
      if (epoch !== this.epoch) return;
      const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429;
      this.changeRoom(id, (room) => markDelivery(room, draft.client_message_id, rejected ? "rejected" : "unknown",
        rejected ? messageOf(error) : "저장 여부를 확인하지 못했습니다. 같은 요청 번호로 다시 확인해 주세요."));
    }
  }
}
