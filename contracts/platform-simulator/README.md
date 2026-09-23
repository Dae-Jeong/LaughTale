# 모의 플랫폼 계약 · v1

실제 플랫폼 API가 아닌 로컬 실험의 공통 wire 계약입니다. Python의 유일한 정의는
`src/platform_contracts/wire.py`이며 Chat과 Mock이 이 패키지를 의존합니다.
`schemas.json`은 Pydantic 생성 결과로 직접 수정하지 않습니다. 업무 정책·DB 객체는 포함하지 않습니다.

## 연결 기준

| 제공자 | 경로 | 의미 |
| --- | --- | --- |
| Chat | `POST /v1/external-events` | InboundEvent 저장 후 201, replay 200, 충돌 409 |
| Mock | `POST /mock/v1/messages` | OutboundCommand 접수 후 `data: AcceptedEffect`, 신규201/replay200 |
| Mock | `GET /mock/v1/operations/{operation_id}?run_id=...` | lookup 지원 시 효과 조회, 없음404·미지원405 |
| Mock | `POST /control/v1/runs` | RunConfig 등록201, 같은 run 재등록은 충돌409 |
| Mock | `POST /control/v1/runs/{run_id}/events` | InboundEvent 등록201/replay200·다른 본문409 |
| Mock | `POST /control/v1/runs/{run_id}/events/{event_id}/deliver` | 명시 전달 1회, 자동 무한 재시도 없음 |
| Mock | `GET /control/v1/runs/{run_id}/ledger?after=0&limit=100` | 시험용 원장 조회. Chat 업무가 소비하지 않습니다. |

Mock 제어용 Bearer와 Chat 발신용 Bearer를 분리합니다. Mock의 Chat 수신 호출은 설정된
연결별 Bearer를 사용하며 Chat은 그 자격증명에 지정된 connection/profile만 허용합니다.
secret은 schema·fixture·로그에 넣지 않습니다. URL은 운영자 설정이며 메시지 입력에서 받지 않습니다.

ID는 합성용 ASCII 1–128자, 본문은 원문 보존 1–2000코드포인트이며 공백만·NUL·surrogate를 거절합니다.
시각은 aware datetime입니다. HTTP 전체 body는 별도로16KiB·수신5초 제한을 적용합니다.
seq는 Chat API에서 bigint 범위 십진 문자열입니다. frozen DTO의 model_copy(update=...)는 검증을 건너뛰므로 신뢰하지 않은 입력에 사용하지 않습니다.

`external_event_id`는 이벤트 중복, `external_message_id`는 메시지 중복 식별자입니다.
업무 중복 범위는 연결 계정과 방을 포함합니다. `run_id`는 추적용이며 연결 ID를 run별로 준비합니다.
최초 모의 이벤트 제어 경로에서는 event_id를 run 안에서 고유하게 생성합니다.

`pending/sending/accepted/rejected/unknown`은 발신 상태입니다. accepted는 외부 접수이지 읽음이 아닙니다.
발신 HTTP 예산은2초, 한 번에1개 작업, lease15초, 최대3시도·신규 발신 착수 기한30초를 초기 로컬 기준으로 둡니다. 이미 착수한 HTTP와 후속 조회는 각각 최대2초가 추가될 수 있어30초가 엄격한 전체 완료 기한은 아닙니다.
확정 미접수의429/503은 Retry-After(최대10초)를 고려하고 1·2·4초 backoff에 jitter를 더합니다.
timeout은 unknown으로 분리하고 멱등·조회 미지원이면 자동 재발송하지 않습니다.
재시작·lease 만료도 이전 외부 효과가 없었다는 근거가 아닙니다.
결과 조회는 즉시1회입니다. 조회 시점 이후 생긴 효과는 자동 대사하지 않으며 멱등 미지원이라면 `unknown`으로 남깁니다. lease token CAS가 새 소유자의 결과를 보호하지만 DB 실행 순간의 엄격한 lease 만료 판정까지 보장하지 않습니다.

RunConfig의 capability와 fault는 합성 시나리오이지 Telegram 등의 실제 지원 능력이 아닙니다.
`delay_after`는 효과 기록 후 지연 응답이며 packet loss 자체를 모사하지 않습니다.
원장의 상한 초과는 명시 거절하며 중복 판정 기록을 silent eviction하지 않습니다.

## 검증

이 디렉터리에서 실행합니다.

```sh
uv tool run --from uv==0.12.10 uv run --locked pytest -q
uv tool run --from uv==0.12.10 uv run --locked python -m platform_contracts.export
uv tool run --from uv==0.12.10 uv run --locked python -m platform_contracts.export --check
```

서비스의 엔드포인트 구현·검증 상태는 각 서비스 README가 소유합니다. 계약 파일 존재만으로 연동 완료가 아닙니다.
