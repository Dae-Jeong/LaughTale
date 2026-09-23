import { ApiError, type Transport } from "./api";
import { sequence, type Actor } from "./model";
import { parseActor } from "./wire";
import { appendExternal, parseExternalHead, parseExternalHistory, parseExternalMessage, parseExternalRooms,
  type ExternalHistory, type ExternalMessage, type ExternalRoom } from "./external-wire";

export type ExternalPending = { id: string; text: string; room: string; sender: string; state: "sending" | "unknown" | "rejected" };
export type ExternalRoomState = { messages: ExternalMessage[]; pending?: ExternalPending; head: string; syncing: boolean; error?: string };
export type ExternalState = { actor?: Actor; rooms: ExternalRoom[]; selected?: string; roomStates: Record<string, ExternalRoomState>; busy: boolean; connected: boolean; error?: string };
export const initialExternalState: ExternalState = { rooms: [], roomStates: {}, busy: false, connected: false };
export type ExternalDecoder = {
  actor(value: unknown): Actor;
  rooms(value: unknown): ExternalRoom[];
  history(value: unknown): ExternalHistory;
  message(value: unknown): ExternalMessage;
};
const decoder: ExternalDecoder = { actor: parseActor, rooms: parseExternalRooms, history: parseExternalHistory, message: parseExternalMessage };
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.";

export class ExternalChatController {
  private state: ExternalState = initialExternalState;
  private listeners = new Set<() => void>();
  private epoch = 0;
  private requests = new Set<AbortController>();
  private sessionQueue: Promise<void> = Promise.resolve();
  private sessionAbort?: AbortController;
  private ws?: WebSocket;
  private pollTimer?: ReturnType<typeof setTimeout>;
  private retryTimer?: ReturnType<typeof setTimeout>;
  private handshakeTimer?: ReturnType<typeof setTimeout>;
  private reconnectAttempts = 0;
  private listFailures = 0;
  private listing = false;
  private failures = new Map<string, number>();
  private syncs = new Map<string, Promise<void>>();
  private statusCursors = new Map<string, string>();

  constructor(private transport: Transport, private randomId = () => crypto.randomUUID(), private decode: ExternalDecoder = decoder) {}
  getSnapshot = (): ExternalState => this.state;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private update(change: Partial<ExternalState>): void {
    this.state = { ...this.state, ...change };
    this.listeners.forEach((listener) => listener());
  }
  private changeRoom(id: string, change: Partial<ExternalRoomState>): void {
    const room = this.state.roomStates[id];
    if (room) this.update({ roomStates: { ...this.state.roomStates, [id]: { ...room, ...change } } });
  }
  private stopSocket(): void {
    clearTimeout(this.retryTimer); clearTimeout(this.handshakeTimer);
    const socket = this.ws; this.ws = undefined; socket?.close();
    this.update({ connected: false });
  }
  private invalidate(): number {
    this.epoch++;
    clearTimeout(this.pollTimer); this.stopSocket();
    this.requests.forEach((request) => request.abort()); this.requests.clear();
    this.syncs.clear(); this.failures.clear(); this.statusCursors.clear(); this.listFailures = 0; this.listing = false; this.reconnectAttempts = 0;
    this.state = initialExternalState;
    this.update(initialExternalState);
    return this.epoch;
  }
  dispose(): void { this.invalidate(); this.sessionAbort?.abort(); }
  private async request(path: string, epoch: number, body?: unknown, signal?: AbortSignal): Promise<unknown> {
    const abort = new AbortController(); this.requests.add(abort);
    const timer = setTimeout(() => abort.abort(), 10_000);
    try {
      const result = await this.transport.request(path, { body, signal: signal ? AbortSignal.any([abort.signal, signal]) : abort.signal });
      if (epoch !== this.epoch || abort.signal.aborted || signal?.aborted) throw new Error("종료된 요청입니다.");
      return result.body;
    } finally { clearTimeout(timer); this.requests.delete(abort); }
  }
  async start(): Promise<void> {
    const epoch = this.epoch;
    this.update({ busy: true });
    try {
      const actor = this.decode.actor(await this.request("/v1/session", epoch));
      if (epoch === this.epoch) await this.activate(actor, epoch);
    } catch (cause) {
      if (epoch === this.epoch && !(cause instanceof ApiError && cause.status === 401)) this.update({ error: errorText(cause) });
    } finally { if (epoch === this.epoch) this.update({ busy: false }); }
  }
  switchUser(user: "user_a" | "user_b"): Promise<void> {
    const epoch = this.invalidate(); this.update({ busy: true });
    const next = this.sessionQueue.then(async () => {
      if (epoch !== this.epoch) return;
      // Do not abort an older in-flight Set-Cookie request merely to start a newer
      // one. Await its settlement, then issue the current user's cookie request.
      const abort = new AbortController();
      this.sessionAbort = abort;
      const timeout = setTimeout(() => abort.abort(), 10_000);
      try {
        const { body } = await this.transport.request("/v1/dev/session", { body: { user }, signal: abort.signal });
        if (epoch === this.epoch && !abort.signal.aborted) await this.activate(this.decode.actor(body), epoch);
      } catch (cause) { if (epoch === this.epoch) this.update({ error: errorText(cause) }); }
      finally { clearTimeout(timeout); if (this.sessionAbort === abort) this.sessionAbort = undefined; if (epoch === this.epoch) this.update({ busy: false }); }
    });
    this.sessionQueue = next;
    return next;
  }
  private async activate(actor: Actor, epoch: number): Promise<void> {
    this.update({ actor });
    await this.loadRooms(epoch);
    if (epoch === this.epoch) this.schedulePoll(epoch);
  }
  private async loadRooms(epoch: number): Promise<void> {
    if (this.listing || this.listFailures >= 3) return;
    this.listing = true;
    try {
      const rooms = this.decode.rooms(await this.request("/v1/external-conversations", epoch));
      if (epoch !== this.epoch) return;
      if (rooms.length > 1000 || new Set(rooms.map((room) => room.conversation_id)).size !== rooms.length) throw new Error("대화 목록 식별자 또는 상한이 일치하지 않습니다.");
      const roomStates = { ...this.state.roomStates };
      for (const room of rooms) {
        sequence(room.head_seq);
        roomStates[room.conversation_id] ??= { messages: [], head: "0", syncing: false };
        const previous = roomStates[room.conversation_id];
        if (BigInt(room.head_seq) > BigInt(previous.head)) roomStates[room.conversation_id] = { ...previous, head: room.head_seq };
      }
      // Keep unresolved requests for removed rooms; do not silently erase them.
      for (const id of Object.keys(roomStates)) if (!rooms.some((room) => room.conversation_id === id) && !roomStates[id].pending) delete roomStates[id];
      this.update({ rooms, roomStates }); this.listFailures = 0;
      if (!rooms.some((room) => room.conversation_id === this.state.selected)) {
        this.stopSocket(); this.update({ selected: undefined });
        if (rooms[0]) this.selectRoom(rooms[0].conversation_id);
      }
    } catch (cause) {
      if (epoch === this.epoch) { this.listFailures++; this.update({ error: errorText(cause) }); }
    } finally { if (epoch === this.epoch) this.listing = false; }
  }
  private schedulePoll(epoch: number): void {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(async () => {
      if (epoch !== this.epoch || !this.state.actor) return;
      await this.loadRooms(epoch);
      if (epoch !== this.epoch) return;
      if (this.state.selected) await this.sync(this.state.selected, epoch);
      if (epoch === this.epoch) this.schedulePoll(epoch);
    }, 5000);
  }
  selectRoom(id: string): void {
    if (this.state.selected === id || !this.state.rooms.some((room) => room.conversation_id === id)) return;
    this.stopSocket(); this.reconnectAttempts = 0;
    this.update({ selected: id, error: undefined });
    this.connect(this.epoch, id);
    void this.sync(id, this.epoch);
  }
  private connect(epoch: number, room: string): void {
    if (epoch !== this.epoch || this.state.selected !== room) return;
    let socket: WebSocket;
    try { socket = this.transport.socket(); }
    catch { this.scheduleReconnect(epoch, room); return; }
    this.ws = socket;
    const valid = () => epoch === this.epoch && this.ws === socket && this.state.selected === room;
    this.handshakeTimer = setTimeout(() => { if (valid()) socket.close(); }, 10_000);
    socket.onopen = () => { if (valid()) socket.send(JSON.stringify({ type: "subscribe", conversation_id: room })); };
    socket.onmessage = (event) => {
      if (!valid()) return;
      try {
        if (typeof event.data !== "string" || event.data.length > 16384) throw new Error("외부 채팅 신호 크기가 올바르지 않습니다.");
        const value = parseExternalHead(JSON.parse(event.data));
        if (value.conversation_id !== room) throw new Error("외부 채팅 구독 신호가 일치하지 않습니다.");
        const head = sequence(value.head_seq);
        const previous = this.state.roomStates[room];
        // A HTTP list response may observe a newer commit before an older WS frame arrives.
        if (BigInt(head) > BigInt(previous.head)) this.changeRoom(room, { head });
        clearTimeout(this.handshakeTimer); this.update({ connected: true });
        void this.sync(room, epoch);
      } catch (cause) { this.update({ error: errorText(cause) }); socket.close(); }
    };
    socket.onclose = () => {
      if (!valid()) return;
      clearTimeout(this.handshakeTimer); this.ws = undefined; this.update({ connected: false }); this.scheduleReconnect(epoch, room);
    };
    socket.onerror = () => { if (valid()) this.update({ error: "실시간 연결을 확인해 주세요." }); };
  }
  private scheduleReconnect(epoch: number, room: string): void {
    if (this.reconnectAttempts >= 6) { this.update({ error: "자동 재연결을 멈췄습니다. 다시 동기화를 눌러 주세요." }); return; }
    this.retryTimer = setTimeout(() => this.connect(epoch, room), Math.min(1000 * 2 ** this.reconnectAttempts++, 15000));
  }
  recover = (): void => {
    if (!this.state.actor) return;
    this.listFailures = 0; this.failures.clear(); this.reconnectAttempts = 0;
    void this.loadRooms(this.epoch);
    if (this.state.selected) {
      if (!this.ws || this.ws.readyState !== 1) { this.stopSocket(); this.connect(this.epoch, this.state.selected); }
      void this.sync(this.state.selected, this.epoch);
    }
  };
  private sync(id: string, epoch: number): Promise<void> {
    const existing = this.syncs.get(id);
    if (existing) return existing;
    if ((this.failures.get(id) ?? 0) >= 3 || epoch !== this.epoch) return Promise.resolve();
    const work = this.performSync(id, epoch);
    this.syncs.set(id, work);
    void work.finally(() => { if (this.syncs.get(id) === work) this.syncs.delete(id); });
    return work;
  }
  private async performSync(id: string, epoch: number): Promise<void> {
    const room = this.state.roomStates[id]; if (!room) return;
    this.changeRoom(id, { syncing: true });
    const budget = new AbortController(); const timer = setTimeout(() => budget.abort(), 15_000);
    try {
      let current = room.messages; let snapshot: string | undefined; let complete = false;
      for (let pageNo = 0; pageNo < 50; pageNo++) {
        const after = current.at(-1)?.seq ?? "0";
        const query = new URLSearchParams({ after_seq: after, limit: "100" });
        if (snapshot !== undefined) query.set("snapshot_head_seq", snapshot);
        const page = this.decode.history(await this.request(`/v1/external-conversations/${id}/messages?${query}`, epoch, undefined, budget.signal));
        if (epoch !== this.epoch) return;
        sequence(page.meta.next_cursor); sequence(page.meta.snapshot_head_seq);
        snapshot ??= page.meta.snapshot_head_seq;
        if (snapshot !== page.meta.snapshot_head_seq || BigInt(snapshot) < BigInt(after) || (page.meta.has_more && !page.data.length)) throw new Error("내역 페이지 경계가 일치하지 않습니다.");
        current = appendExternal(id, current, page.data);
        if (Object.entries(this.state.roomStates).reduce((count, [roomId, value]) => count + (roomId === id ? current.length : value.messages.length), 0) > 20_000) throw new Error("전체 화면 내역20,000건 상한에 도달했습니다.");
        const last = current.at(-1)?.seq ?? "0";
        if (last !== page.meta.next_cursor || BigInt(last) > BigInt(snapshot) || page.meta.has_more !== (BigInt(last) < BigInt(snapshot))) throw new Error("내역 커서 또는 누락 구간이 일치하지 않습니다.");
        const knownHead = this.state.roomStates[id].head;
        const pending = this.state.roomStates[id].pending;
        const found = pending && current.find((message) => message.sender_kind === "operator" && message.client_message_id === pending.id && message.sender_id === pending.sender);
        if (found && (found.text !== pending.text || found.conversation_id !== pending.room)) throw new Error("저장된 요청의 본문이 일치하지 않습니다.");
        this.changeRoom(id, { messages: current, pending: found ? undefined : pending, head: BigInt(snapshot) > BigInt(knownHead) ? snapshot : knownHead });
        if (!page.meta.has_more) { complete = true; break; }
      }
      if (!complete) throw new Error("내역 복구 페이지 상한에 도달했습니다.");
      const unresolved = current.filter((message) => message.operation_id && !["accepted", "rejected"].includes(message.delivery_state ?? ""));
      const statusCursor = BigInt(this.statusCursors.get(id) ?? "0");
      const batch = [...unresolved.filter((message) => BigInt(message.seq) > statusCursor), ...unresolved.filter((message) => BigInt(message.seq) <= statusCursor)].slice(0, 20);
      for (const original of batch) {
        // Advance even across unknown/failing entries; the next bounded pass rotates
        // to later operations instead of letting an old unknown monopolize the queue.
        this.statusCursors.set(id, original.seq);
        const latest = this.decode.message(await this.request(`/v1/external-conversations/${id}/messages/${original.message_id}`, epoch, undefined, budget.signal));
        if (epoch !== this.epoch) return;
        const immutable = Object.keys(original).filter((key) => key !== "delivery_state" && key !== "effect_id") as (keyof ExternalMessage)[];
        if (immutable.some((key) => original[key] !== latest[key]) || latest.delivery_state === null || (original.effect_id !== null && original.effect_id !== latest.effect_id)) throw new Error("발신 상태의 원본 메시지가 일치하지 않습니다.");
        current = current.map((item) => item.message_id === latest.message_id ? latest : item);
        this.changeRoom(id, { messages: current });
      }
      if (epoch !== this.epoch) return;
      const pending = this.state.roomStates[id]?.pending;
      const found = pending && current.find((message) => message.client_message_id === pending.id && message.sender_id === pending.sender);
      if (found && (found.text !== pending.text || found.conversation_id !== pending.room)) throw new Error("저장된 요청의 본문이 일치하지 않습니다.");
      this.changeRoom(id, { messages: current, pending: found ? undefined : pending, error: undefined }); this.failures.set(id, 0);
    } catch (cause) {
      if (epoch === this.epoch) { this.failures.set(id, (this.failures.get(id) ?? 0) + 1); this.changeRoom(id, { error: errorText(cause) }); }
    } finally { clearTimeout(timer); if (epoch === this.epoch) this.changeRoom(id, { syncing: false }); }
  }
  async send(text: string, retry = false): Promise<void> {
    const { actor, selected, busy } = this.state;
    if (!actor || !selected || busy) return;
    const previous = this.state.roomStates[selected]?.pending;
    if (retry ? previous?.state !== "unknown" : !!previous) return;
    const pending: ExternalPending = retry && previous ? { ...previous, state: "sending" } : { id: this.randomId(), text, room: selected, sender: actor.user_id, state: "sending" };
    if (!pending.text.trim() || [...pending.text].length > 2000 || /[\u0000\uD800-\uDFFF]/u.test(pending.text)) return;
    if (Object.values(this.state.roomStates).filter((room) => room.pending).length >= 100 && !previous) return;
    const epoch = this.epoch;
    this.changeRoom(selected, { pending, error: undefined });
    try {
      const message = this.decode.message(await this.request(`/v1/external-conversations/${selected}/messages`, epoch, { client_message_id: pending.id, text: pending.text }));
      if (message.client_message_id !== pending.id || message.sender_id !== pending.sender || message.text !== pending.text || message.conversation_id !== pending.room || message.sender_kind !== "operator") throw new Error("전송 응답이 원래 요청과 일치하지 않습니다.");
      if (epoch !== this.epoch) return;
      if (this.state.roomStates[selected]?.pending?.id === pending.id) this.changeRoom(selected, { pending: undefined });
      // Only authoritative history appends sequence; a late ACK may arrive with a gap.
      void this.sync(selected, epoch);
    } catch (cause) {
      if (epoch !== this.epoch) return;
      const current = this.state.roomStates[selected]?.pending;
      if (!current || current.id !== pending.id) return; // History may already prove storage.
      const rejected = cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408;
      this.changeRoom(selected, { pending: { ...pending, state: rejected ? "rejected" : "unknown" }, error: errorText(cause) });
    }
  }
  dismissRejected(): void {
    const id = this.state.selected;
    if (id && this.state.roomStates[id]?.pending?.state === "rejected") this.changeRoom(id, { pending: undefined });
  }
}
