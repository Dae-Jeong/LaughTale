"use client";

import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import { browserTransport } from "./api";
import { ChatController, initialState } from "./controller";
import { needsRecovery, type Delivery } from "./model";

const apiOrigin = process.env.NEXT_PUBLIC_CHAT_API_ORIGIN ?? "http://127.0.0.1:18082";
const gatewayOrigin = process.env.NEXT_PUBLIC_CHAT_GATEWAY_ORIGIN ?? apiOrigin;
const statusLabels: Record<Delivery, string> = { sending: "전송 중", stored: "저장됨", unknown: "결과 확인 필요", rejected: "거절됨" };
const connectionLabels = { offline: "연결 전", connecting: "연결 중", connected: "실시간 연결됨", reconnecting: "재연결 중", stopped: "연결 끊김" };

export function ChatPlayground() {
  const [controller] = useState(() => new ChatController(browserTransport(apiOrigin, gatewayOrigin)));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, () => initialState);
  const [draft, setDraft] = useState("");
  const bottom = useRef<HTMLLIElement>(null);
  const room = state.selected ? state.roomStates[state.selected] : undefined;
  const conversation = state.rooms.find((item) => item.conversation_id === state.selected);
  const pending = room?.outgoing.filter((item) => item.state !== "stored") ?? [];
  const count = [...draft].length;
  useEffect(() => {
    void controller.start();
    const focus = () => { if (document.visibilityState === "visible") controller.focus(); };
    window.addEventListener("focus", focus);
    window.addEventListener("online", focus);
    document.addEventListener("visibilitychange", focus);
    return () => {
      window.removeEventListener("focus", focus);
      window.removeEventListener("online", focus);
      document.removeEventListener("visibilitychange", focus);
      controller.dispose();
    };
  }, [controller]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest" }); }, [room?.messages.length, pending.length]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!draft.trim() || count > 2000 || !state.actor || !state.selected || pending.length >= 100) return;
    void controller.send(draft);
    setDraft("");
  }
  function switchUser(user: "user_a" | "user_b"): void {
    setDraft("");
    void controller.switchUser(user);
  }
  return (
    <main className="workspace">
      <header className="masthead">
        <h1>Laughtale <span>Chat</span></h1>
        <p className="environment">로컬 실험 · 합성 데이터</p>
      </header>
      <aside className="sidebar" aria-label="사용자와 대화방">
        <section className="identity" aria-labelledby="identity-title">
          <h2 id="identity-title">합성 사용자</h2>
          <p>{state.sessionBusy ? "세션을 확인하고 있습니다." : state.actor ? `${state.actor.display_name} 님으로 참여 중입니다.` : "참여할 사용자를 선택해 주세요."}</p>
          <div className="persona-buttons">
            <button type="button" onClick={() => switchUser("user_a")} disabled={state.sessionBusy}>user_a</button>
            <button type="button" onClick={() => switchUser("user_b")} disabled={state.sessionBusy}>user_b</button>
          </div>
          <small>실제 인증이 아닙니다. 두 사용자 간 대화는 서로 독립된 브라우저에서 확인해 주세요.</small>
        </section>
        <nav aria-labelledby="rooms-title" className="rooms">
          <h2 id="rooms-title">참여 중인 대화 <span>{state.rooms.length}</span></h2>
          {state.rooms.length ? <ul>{state.rooms.map((item) => (
            <li key={item.conversation_id}>
              <button type="button" aria-current={item.conversation_id === state.selected ? "page" : undefined}
                onClick={() => { setDraft(""); controller.selectRoom(item.conversation_id); }}>
                <span>{item.title}</span><small>내부 DM</small>
              </button>
            </li>
          ))}</ul> : <p className="muted">{state.actor ? "참여 중인 대화가 없습니다." : "사용자를 선택하면 대화 목록을 불러옵니다."}</p>}
        </nav>
        <p className="sidebar-note">텍스트 메시지만 지원합니다.<br />저장됨은 읽음 상태가 아닙니다.</p>
      </aside>
      <section className="conversation" aria-labelledby="conversation-title">
        <header className="conversation-header">
          <h2 id="conversation-title">{conversation?.title ?? "대화를 시작해 보세요"}</h2>
          <span className={`connection ${state.connection}`} role="status">{connectionLabels[state.connection]}</span>
        </header>
        {state.error && <p className="error-banner" role="alert">{state.error}</p>}
        {room?.error && <p className="error-banner" role="alert">{room.error}</p>}
        {conversation && room ? <>
          <div className="sync-strip">
            <span role="status">{room.syncing ? "메시지 내역을 동기화하고 있습니다." : needsRecovery(room) ? "누락된 메시지를 확인하고 있습니다." : "현재 확인한 내역이 모두 연결되어 있습니다."}</span>
            <button type="button" className="text-button" onClick={controller.focus} disabled={room.syncing}>내역 동기화</button>
          </div>
          <ol className="messages" aria-label="메시지 내역" aria-live="polite" aria-relevant="additions text">
            {!room.messages.length && !pending.length && <li className="empty-message">{room.syncing ? "내역을 불러오고 있습니다." : "아직 표시할 메시지가 없습니다. 첫 인사를 보내 주세요."}</li>}
            {room.messages.map((message) => (
              <li key={message.message_id} className={message.sender_id === state.actor?.user_id ? "message mine" : "message"}>
                <header><strong>{message.sender_id === state.actor?.user_id ? "나" : "상대방"}</strong>
                  <time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false })}</time>
                  <span className="message-state">저장됨</span></header>
                <p>{message.text}</p>
              </li>
            ))}
            {pending.map((message) => <li key={message.client_message_id} className={`message mine ${message.state}`}>
              <header><strong>나</strong><span className="message-state">{statusLabels[message.state]}</span></header>
              <p>{message.text}</p>
              {message.error && <small>{message.error}</small>}
              {message.state === "unknown" && <button type="button" className="text-button" onClick={() => void controller.retry(message.client_message_id)}>같은 요청으로 재시도</button>}
            </li>)}
            <li ref={bottom} className="scroll-anchor" aria-hidden="true" />
          </ol>
          <form className="composer" onSubmit={submit}>
            <label htmlFor="message">메시지</label>
            <textarea id="message" rows={3} value={draft} onChange={(event) => setDraft(event.target.value)}
              placeholder="메시지를 입력해 주세요." aria-describedby="message-help" aria-invalid={count > 2000} />
            <footer><small id="message-help">{pending.length >= 100 ? "대기 중인 메시지 한도에 도달했습니다. 저장 결과를 먼저 확인해 주세요." : `${count.toLocaleString("ko-KR")} / 2,000자 · 줄바꿈은 Enter를 눌러 주세요.`}</small>
              <button type="submit" className="primary" disabled={!draft.trim() || count > 2000 || state.sessionBusy || pending.length >= 100}>보내기</button></footer>
          </form>
        </> : <div className="welcome"><p>같은 대화방에서 메시지를 주고받고,<br />연결이 돌아오면 누락된 내역을 이어서 확인합니다.</p></div>}
        <footer className="diagnostics">
          <span>연속 seq <b>{room?.contiguous ?? "—"}</b> / 확인한 head <b>{room?.head ?? "—"}</b></span>
          {state.actor && <button type="button" className="text-button" onClick={controller.reconnect}>연결 재시도</button>}
          <details><summary>연결 상세</summary><dl><dt>API</dt><dd>{apiOrigin}</dd><dt>최근 요청 ID</dt><dd>{state.requestId ?? "아직 없습니다."}</dd><dt>대기 이벤트</dt><dd>{room ? Object.keys(room.buffer).length : 0}</dd></dl></details>
        </footer>
      </section>
    </main>
  );
}
