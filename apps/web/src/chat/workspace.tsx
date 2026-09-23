"use client";
import { useState } from "react";
import { ChatPlayground } from "./playground";
import { ExternalPlayground } from "./external-playground";

export function ChatWorkspace() {
  const [source, setSource] = useState<"internal" | "external">("internal");
  return <div className="chat-shell"><nav className="source-tabs" aria-label="대화 출처">
    <button aria-pressed={source === "internal"} onClick={() => setSource("internal")}>내부 채팅</button>
    <button aria-pressed={source === "external"} onClick={() => setSource("external")}>외부 통합 채팅</button>
  </nav>{source === "internal" ? <ChatPlayground /> : <ExternalPlayground />}</div>;
}
