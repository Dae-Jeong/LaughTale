"use client";

import { useEffect, useState, useSyncExternalStore, type FormEvent } from "react";
import { browserTransport } from "./api";
import { ExternalChatController, initialExternalState } from "./external-controller";

const apiOrigin = process.env.NEXT_PUBLIC_CHAT_API_ORIGIN ?? "http://127.0.0.1:18082";
const labels = { pending: "발신 대기", sending: "외부 전송 중", accepted: "외부 접수됨", rejected: "외부 거절됨", unknown: "외부 결과 확인 필요" };

export function ExternalPlayground() {
  const [controller] = useState(() => new ExternalChatController({
    ...browserTransport(apiOrigin),
    socket() { const url = new URL("/v1/external-ws", apiOrigin); url.protocol = url.protocol === "https:" ? "wss:" : "ws:"; return new WebSocket(url); },
  }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, () => initialExternalState);
  const [filter, setFilter] = useState("all");
  const [draft, setDraft] = useState("");
  const room = state.selected ? state.roomStates[state.selected] : undefined;
  const selectedRoom = state.rooms.find((item) => item.conversation_id === state.selected);
  const pending = room?.pending;
  const hiddenPending = Object.entries(state.roomStates).filter(([id, item]) => id !== state.selected && item.pending);
  useEffect(() => {
    void controller.start();
    const focus = () => { if (document.visibilityState === "visible") controller.recover(); };
    window.addEventListener("online", focus); window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", focus);
    return () => { window.removeEventListener("online", focus); window.removeEventListener("focus", focus); document.removeEventListener("visibilitychange", focus); controller.dispose(); };
  }, [controller]);
  function send(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!draft.trim() || [...draft].length > 2000 || !state.actor || !state.selected || pending) return;
    void controller.send(draft); setDraft("");
  }
  return <main className="workspace">
    <header className="masthead"><h1>Laughtale <span>통합 외부 채팅</span></h1><p className="environment">7채널 Mock · 실제 플랫폼 연동이 아닙니다.</p></header>
    <aside className="sidebar">
      <section className="identity"><h2>합성 상담원</h2><p>{state.actor ? `${state.actor.display_name} 님으로 참여 중입니다.` : "사용자를 선택해 주세요."}</p>
        <div className="persona-buttons">{(["user_a", "user_b"] as const).map((user) => <button key={user} disabled={state.busy} onClick={() => { setDraft(""); void controller.switchUser(user); }}>{user}</button>)}</div>
        <label htmlFor="channel">채널 </label><select id="channel" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">전체 채널</option>{[...new Set(state.rooms.map((item) => item.profile))].map((profile) => <option key={profile}>{profile}</option>)}</select>
      </section>
      <nav className="rooms" aria-label="외부 대화 목록"><h2>외부 대화 <span>{state.rooms.length}</span></h2><ul>{state.rooms.filter((item) => filter === "all" || item.profile === filter).map((item) => <li key={item.conversation_id}><button aria-current={state.selected === item.conversation_id ? "page" : undefined} onClick={() => { if (state.selected !== item.conversation_id) setDraft(""); controller.selectRoom(item.conversation_id); }}><span>{item.title}</span><small>{item.profile}{state.roomStates[item.conversation_id]?.pending ? " · 요청 확인 중" : ""}</small></button></li>)}</ul>{!state.rooms.length && <p className="muted">제어 도구에서 합성 연결을 등록하면 여기에 표시됩니다.</p>}</nav>
      {hiddenPending.length > 0 && <p role="status">다른 대화 {hiddenPending.length}곳에 저장 결과 확인이 필요한 요청이 남아 있습니다. 해당 대화로 돌아가 확인해 주세요.</p>}
      <p className="sidebar-note">외부 접수는 읽음 상태가 아닙니다.<br />결과 불명 메시지는 새 ID로 자동 재전송하지 않습니다.</p>
    </aside>
    <section className="conversation" aria-label="외부 대화">
      <header className="conversation-header"><div><h2>{selectedRoom?.title ?? "외부 대화를 선택해 주세요"}</h2>{selectedRoom && <small title={selectedRoom.connection_id}>{selectedRoom.profile} · 연결 {selectedRoom.connection_id.slice(0, 8)} · {selectedRoom.external_conversation_id}</small>}</div><span className={`connection ${state.connected ? "connected" : "reconnecting"}`} role="status">{state.connected ? "실시간 연결됨" : "연결 확인 중 · 제한된 주기적 복구"}</span></header>
      {(state.error || room?.error) && <p className="error-banner" role="alert">{room?.error ?? state.error}</p>}
      <ol className="messages" aria-label="외부 메시지 내역" aria-live="polite">{room?.messages.map((message) => <li key={message.message_id} className={`message ${message.sender_kind === "operator" ? "mine" : ""} ${message.delivery_state ?? ""}`}><header><strong>{message.sender_kind === "customer" ? "외부 고객" : "상담원"}</strong><time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString("ko-KR")}</time><span>{message.delivery_state ? labels[message.delivery_state] : "수신 저장됨"}</span></header><p>{message.text}</p></li>)}</ol>
      {pending && <div className="sync-strip" role="status"><p>{pending.text}</p><p>{pending.state === "unknown" ? "저장 응답을 확인하지 못했습니다. 같은 ID로만 다시 확인합니다." : pending.state === "rejected" ? "저장 요청이 거절되었습니다." : "저장을 요청하고 있습니다."}</p>{pending.state === "unknown" && <button onClick={() => void controller.send("", true)}>같은 요청으로 확인</button>}{pending.state === "rejected" && <button onClick={() => { setDraft(pending.text); controller.dismissRejected(); }}>본문 수정하기</button>}</div>}
      <form className="composer" onSubmit={send}><label htmlFor="external-message">외부 플랫폼에 답장</label><textarea id="external-message" value={draft} onChange={(event) => setDraft(event.target.value)} disabled={!state.selected || !!pending} /><footer><small>{[...draft].length} / 2,000자 · 저장과 외부 접수를 구분합니다.</small><button className="primary" disabled={!state.selected || !state.actor || state.busy || !draft.trim() || [...draft].length > 2000 || !!pending}>답장</button></footer></form>
      <footer className="diagnostics"><span>연속 seq {room?.messages.at(-1)?.seq ?? "0"} / head {room?.head ?? "0"}{room?.syncing ? " · 동기화 중" : ""}</span><button className="text-button" onClick={controller.recover}>다시 동기화</button></footer>
    </section>
  </main>;
}
