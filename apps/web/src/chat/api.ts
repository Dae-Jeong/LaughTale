import { object } from "./wire";

export class ApiError extends Error {
  constructor(public status: number, public code: string, public requestId?: string) {
    super(code === "UNAUTHENTICATED" ? "합성 사용자를 다시 선택해 주세요." :
      code === "CONVERSATION_NOT_FOUND" ? "대화를 찾을 수 없거나 참여 권한이 없습니다." :
      code === "IDEMPOTENCY_CONFLICT" ? "같은 요청 번호의 본문이 달라 전송이 거절되었습니다." :
      status === 422 ? "입력 내용을 확인해 주세요." : "요청을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.");
  }
}
export type RequestResult = { body: unknown; requestId?: string };
export type Transport = {
  request(path: string, options: { signal: AbortSignal; body?: unknown }): Promise<RequestResult>;
  socket(): WebSocket;
};

export function browserTransport(base: string, gatewayBase = base): Transport {
  const origin = new URL(base);
  if (!["http:", "https:"].includes(origin.protocol)) throw new Error("API 주소를 확인해 주세요.");
  const gateway = new URL(gatewayBase);
  if (!["http:", "https:"].includes(gateway.protocol) || gateway.hostname !== origin.hostname ||
    gateway.protocol !== origin.protocol || origin.username || origin.password || gateway.username || gateway.password) {
    throw new Error("API와 Gateway는 같은 호스트와 보안 프로토콜을 사용해야 합니다.");
  }
  return {
    async request(path, { signal, body }) {
      const response = await fetch(new URL(path, origin), {
        method: body === undefined ? "GET" : "POST", credentials: "include", cache: "no-store",
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), signal,
      });
      const requestId = response.headers.get("x-request-id") ?? undefined;
      const result: unknown = await response.json();
      if (!response.ok) {
        const problem = object(result);
        throw new ApiError(response.status, typeof problem.code === "string" ? problem.code : "REQUEST_FAILED",
          typeof problem.request_id === "string" ? problem.request_id : requestId);
      }
      return { body: result, requestId };
    },
    socket() {
      const url = new URL("/v1/ws", gateway);
      url.protocol = gateway.protocol === "https:" ? "wss:" : "ws:";
      return new WebSocket(url);
    },
  };
}
