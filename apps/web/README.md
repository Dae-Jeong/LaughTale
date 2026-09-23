# Laughtale Chat Web

내부 DM과 외부 통합 채팅을 출처 탭으로 나누는 로컬 합성 사용자 실험 화면입니다. `/`에서 `user_a` 또는
`user_b`를 선택하고 실제 chat API와 WebSocket에 직접 연결합니다. Next.js에 DB 접근이나
채팅 업무 서버를 구현하지 않으며 실제 모드의 mock 성공 대체는 없습니다.

## 실행

Node.js 22.9.0, pnpm 10.33.2로 검증했습니다. 저장소 루트에서 실행합니다.

```bash
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web dev
```

주소는 `http://127.0.0.1:18083`입니다. 실행 전에 포트 점유를 확인하고 다른 프로세스를
종료하지 않습니다. 기본 API는 `http://127.0.0.1:18082`이며 변경할 때는
`NEXT_PUBLIC_CHAT_API_ORIGIN`을 개발·빌드 환경에 지정합니다. `localhost`와 `127.0.0.1`을
섞지 않고 BE의 정확한 허용 Origin도 함께 조정합니다.

분산 내부 채팅은 `NEXT_PUBLIC_CHAT_GATEWAY_ORIGIN=http://127.0.0.1:18085`로 WS 목적지를
분리할 수 있습니다(미지정이면 API 주소). Gateway에는 `GATEWAY_BROWSER_PORT=18085`를
설정하고 같은 loopback port로 연결합니다. HTTP/WS는 같은 hostname과 프로토콜을 사용해야
host 전용 세션 쿠키가 공유됩니다. 내부 전달용 token을 브라우저에 넣지 않습니다.

BE는 별도로 준비해야 합니다. 로컬 합성 세션에는 `DEV_SESSIONS_ENABLED=true`와
`DEV_ORIGIN=http://127.0.0.1:18083` 설정이 필요하며, HTTP는 `credentials: include`,
WebSocket은 동일 API host의 session cookie를 사용합니다. DB migration·seed나 인프라
기동은 이 앱 명령에 포함하지 않습니다. BE 설정과 명령은 [chat 서비스](../../services/chat/README.md)를 따릅니다.

두 사용자를 동시에 시험할 때는 쿠키를 공유하지 않는 독립 브라우저/프로필을 사용합니다.
같은 브라우저의 일반 탭 둘은 독립 세션이 아닙니다. 사용자 전환은 기존 요청·소켓·방 상태·초안을 정리합니다.

## 검증과 계약 생성

```bash
pnpm --dir apps/web contracts:generate
pnpm --dir apps/web contracts:check
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web lint
pnpm --dir apps/web build
pnpm --dir apps/web start
```

`contracts:generate`는 BE가 소유한 `contracts/chat/openapi.json`과 `ws-server.schema.json`에서
`src/chat/generated/`의 TypeScript를 생성합니다. 원본 계약이나 생성 파일을 수동 수정하지 않습니다.
`contracts:check`는 원본과 타입 산출물의 차이를 검사합니다. 런타임 응답은 같은 원본 schema를
Ajv로 검증하고, 정수 문자열의 bigint 상한·연속성·메시지 정합성은 소비자 경계에서 확인합니다.

`tests/chat.test.ts`는 생성된 합성 fixture와 명시적 test transport만 사용합니다. 실제 DB·HTTP 서버를
실행하지 않습니다. gap·중복·동일 key 충돌·snapshot 고정·heads 유실 복구·동일 UUID 재시도·사용자
전환 중 늦은 응답·재연결 예산·복구 실패 예산을 검증합니다. Node MockTimers 사용 시험에서
experimental 경고가 발생할 수 있으며 경고를 숨기지 않습니다.

## 내부 DM 상태와 제한

- 전송은 `sending`, `stored`, `unknown`, `rejected`로 구분합니다. timeout이나 5xx는 저장 실패로
  단정하지 않습니다. `unknown` 재시도는 처음 UUID와 본문을 그대로 사용하며 자동으로 쓰기를 재시도하지 않습니다.
- 선택 방의 WS 구독 확인 뒤 history를 읽고, HTTP ACK·WS·history를 한 번만 병합합니다.
  방마다 마지막 연속 seq와 버퍼를 유지하며 gap을 건너뛰지 않습니다. heads, focus, 온라인 복귀,
  재연결이 누락 복구를 시작합니다. 상시 HTTP polling은 없습니다.
- HTTP와 연결·구독 준비 timeout은 각각 10초입니다. WS 재연결은 500ms부터 최대 15초의
  exponential backoff에 0–249ms jitter를 더해 최대 6회 실행합니다. 수동 연결 재시도로 예산을 초기화합니다.
- history는 100건씩 snapshot을 고정해 최대 100페이지를 읽습니다. 연속 실패 3회 뒤 자동 복구를
  멈추며 `내역 동기화`/focus로 재개합니다. 이벤트 버퍼는 방마다 500건, 미확정·거절 전송은 100건으로 제한합니다.
- 현재 세션에서 읽은 메시지는 메모리와 DOM에 보관합니다. 가상 스크롤·장기 캐시·무제한 history
  성능을 검증하지 않았습니다. 대규모 이력에 사용하기 전 표시 윈도우·페이지 UX와 측정이 필요합니다.
- 실제 인증, 전체 대화 관리자 권한, 방 CRUD, 첨부, 읽음, 초안 영구 저장은 제공하지 않습니다.

## 외부 통합 채팅

외부 탭은 같은 상담사 세션으로 참여 권한이 있는 외부 대화를 조회하고 채널별로 필터합니다. [외부 실험 bootstrap](../../tests/system/chat-external/README.md)이 합성 연결·문의·답장을 준비하며 화면에서 제어 자격증명을 보관하거나 임의 연결 권한을 부여하지 않습니다.

- HTTP snapshot/seq 내역이 정본이며 `/v1/external-ws`의 `head`가 동기화를 알립니다. 외부 알림은 BE의 약1초 DB polling 방식입니다. 내역·발신 상태에는 제한된 주기적 HTTP 복구를 함께 사용합니다.
- 우리 DB 저장과 외부의 `pending/sending/accepted/rejected/unknown`을 구분합니다. `accepted`는 읽음이 아닙니다. FE 저장 응답 유실 시에는 같은 UUID·본문으로만 수동 재확인합니다. 외부 재전송 정책은 BE worker가 소유합니다.
- 타입과 HTTP/WS 응답 검증은 `contracts/chat/`의 생성 계약을 소비합니다. 방별 seq·message ID·업무 키·원본 불변 필드를 추가 검사합니다.
- 재연결6회·동기화 연속 실패3회·한 번의 복구15초/50페이지·방5,000건/전체20,000건 표시 상한을 둡니다. 운영용 대규모 이력 UI·가상 스크롤을 의미하지 않습니다.
- 합성 세션은 서버 메모리에 있으므로 Chat 재시작 후 사용자를 다시 선택합니다. 이 화면의 세션·소켓을 다중 Pod 사용자 HA로 설명하지 않습니다.

외부 플랫폼7종은 합성 라벨 프로필입니다. 실제 provider 인증·API 호환성이나 고객 데이터는 포함하지 않습니다.

## 구성과 도구 근거

Next.js 16.3.4 App Router, React 19.2.8, TypeScript 5.9.3, pnpm 10.33.2를 사용합니다.
공식 `create-next-app@16.3.4`의 help 확인 후 `--ts --eslint --app --src-dir --no-tailwind
--no-react-compiler --no-agents-md --empty --use-pnpm --disable-git`로 생성했습니다.
디자인·상태 관리 라이브러리는 추가하지 않았습니다. Ajv/ajv-formats는 응답 검증용이며
openapi-typescript/json-schema-to-typescript와 tsx는 개발 도구입니다. 정확한 설치 버전은 lockfile이 소유합니다.

- [Next.js 생성 CLI](https://nextjs.org/docs/app/api-reference/cli/create-next-app)
- [OpenAPI TypeScript Node API](https://openapi-ts.dev/node)
- [JSON Schema to TypeScript](https://github.com/bcherny/json-schema-to-typescript)
- [Ajv schema 언어](https://ajv.js.org/guide/schema-language)

공식 문서·CLI 확인일: 2026-09-08. 생성기의 ESLint 9 계열 deprecated 안내는 확인했으며,
Next.js 생성 구성과 lock을 유지했습니다. 프레임워크 major 업그레이드는 별도 변경입니다.
