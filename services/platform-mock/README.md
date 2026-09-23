# Platform Mock

외부 계정 없이 통합 대화함의 수신·답장·실패 복구를 시험하는 독립 합성 서버입니다.
Telegram·LINE·Instagram·Facebook·WhatsApp·WeChat·카카오 상담톡은 라벨 프로필이며 실제 API 호환성을 뜻하지 않습니다.
Chat DB에 접근하지 않고 [공통 계약](../../contracts/platform-simulator/README.md)을 소비합니다.

## 실행과 설정

Python 3.14.7, uv 0.12.10을 사용합니다. 이 디렉터리에서 실행합니다.

```sh
uv tool run --from uv==0.12.10 uv sync --locked
uv tool run --from uv==0.12.10 uv run --locked python -m platform_mock.run
```

실행 전에 무시되는 `.env` 또는 환경변수로 다음 값을 준비합니다. 기본 공개 바인딩은 없으며
`127.0.0.1:18087`에만 바인딩합니다. 다른 서비스 점유 여부를 확인한 뒤 기동합니다.

| 설정 | 의미 |
| --- | --- |
| `MOCK_CONTROL_TOKEN` | 제어 API 전용, 최소24자 |
| `MOCK_SERVICE_TOKEN` | Chat 발신 API 전용, 제어 토큰과 다른 최소24자 |
| `MOCK_CHAT_BASE_URL` | 기본 `http://127.0.0.1:18082`, 명시적인 loopback authority만 허용 |
| `MOCK_CONNECTION_TOKENS` | 연결 UUID를 키, Chat 수신 Bearer를 값으로 갖는 JSON 객체 |
| `MOCK_SERVER_PORT` | 기본18087 |
| `MOCK_DELIVERY_TIMEOUT_SECONDS` | 기본2초, 최대5초, 자동 재시도 없음 |
| `MOCK_NETWORK_PROFILE` | 기본 `local`. 명시적 `isolated-lab`만 전체 인터페이스 바인딩·`chat` callback DNS·`platform-mock` Host·현재 단일 node Pod CIDR `10.42.0.0/24`를 추가 허용합니다. |

`isolated-lab`은 [전용 K8s namespace와 NetworkPolicy](../../infra/k8s/chat-external/README.md) 내에서만 사용합니다. Bearer 인증·Origin/forwarded 거절은 유지합니다. CIDR이 다른 클러스터에는 그대로 사용하지 않으며 공개 배포용 설정이 아닙니다.

설정·factory import는 서버나 DB를 만들지 않습니다. lifespan이 HTTP client를 만들고 닫습니다.
서버 실행은1 worker이며 proxy headers와 access log는 비활성화합니다. 제어 토큰·본문을 로그에 출력하지 않습니다.

## 동작과 원장

run 생성 → 합성 이벤트 등록 → event별 `deliver` 호출 → Chat 수신이 기본 흐름입니다.
답장은 `/mock/v1/messages`에서 처리하고 lookup capability에 따라 operation 조회를 지원합니다.
양방향 모두 URL은 서버 설정에서만 정하며 요청 payload의 callback URL은 허용하지 않습니다.

`GET /control/v1/runs/{run_id}/ledger?after=0&limit=100`은 event/effect/attempt를 하나의 append-only cursor로 반환합니다.
`meta.next_cursor`로 다음 페이지를 읽고 `has_more=false`까지 진행합니다. 활성 요청은 나중에 attempt 결과를
기록하므로 대사 시 발생기를 멈추고 `meta.active=0` 확인 후 다시 마지막 cursor부터 읽습니다.
최초 effect와 반복 호출 attempt는 별개이며 accepted는 외부 접수이지 읽음이 아닙니다.
`DELETE /control/v1/runs/{run_id}`는 지정한 run만 삭제하며 활성 요청이 있으면409입니다.

상한은4 runs, 전체 활성 전달/발신16개, 요청 body16KiB·수신5초입니다. 이벤트·시도·효과 상한은 RunConfig가
소유하며 초과는429로 거절합니다. 중복 판정 원장을 조용히 퇴출하지 않습니다.
원장 row는 최대 `max_events + max_effects + max_attempts`입니다. ledger에는 합성 본문이 포함되며
제어 API만 조회할 수 있습니다. 메트릭 저장소·실제 고객 데이터 수집기는 아닙니다.

장애는 operation의 첫 `fault_attempts` 요청에 적용합니다. 429/503/403, 효과 전 지연, 효과 후 지연을 제공합니다.
`delay_after`는 실제 packet loss가 아니라 효과 저장 후 늦은 응답입니다. 멱등 미지원이면 같은 operation 재전송도
별도 효과를 만들고 lookup 지원 시 마지막 효과를 반환합니다. 이 동작은 실제 플랫폼 지원 능력의 주장이 아닙니다.
동일 run/seed/operation/효과순서의 effect ID는 재현되지만 동시 요청 스케줄 자체는 재현되지 않습니다.

**인메모리 원장은 프로세스 재시작 시 사라집니다.** 다중 worker/Pod, Mock 재시작 내구성, K8s, 실제 플랫폼,
대량 부하 발생·성능 보장은 포함하지 않습니다. Chat 재시작 시험에서는 Mock을 유지하고 종료 전 원장을 수집합니다.
별도 부하 발생기가 도착 스케줄을 소유하며 Mock은 명시적인 한 번의 전달만 수행합니다.

## 검증

```sh
uv tool run --from uv==0.12.10 uv run --locked pytest -q
uv tool run --from uv==0.12.10 uv run --locked ruff check .
uv tool run --from uv==0.12.10 uv run --locked ruff format --check .
uv tool run --from uv==0.12.10 uv run --locked ty check
uv tool run --from uv==0.12.10 uv build
```

기본 테스트는 HTTP 대역·ASGI를 사용하며 실제 Chat·DB·외부 플랫폼을 호출하지 않습니다.
Starlette의 기존 BlockingPortal deprecated alias 경고는 숨기지 않습니다.
