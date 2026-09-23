# 외부 채팅 독립 대조기

T1의 오프라인 증거 대조기입니다. Chat·mock 구현을 import하지 않고 Python 표준 라이브러리만 사용합니다.
`oracle.py`는 실제 API 호출·DB 조회·부하 발생·K8s 실행을 하지 않습니다. 아래 `collect.py`는 별도로 승인된 로컬 왕복 실행기입니다. Wire 계약은
[공통 계약](../../../contracts/platform-simulator/src/platform_contracts/wire.py)이 소유하며,
아래 형식은 API가 아니라 시험 수집기가 만드는 정규화된 증거 형식입니다.

## 검증

저장소 루트에서 실행합니다.

```sh
python3 -m unittest discover -s tests/system/chat-external -p 'test_*.py' -v
python3 tests/system/chat-external/oracle.py --help
```

증거 JSON 파일 3개를 준비한 뒤 다음 CLI로 판정합니다. 아래 경로는 사용자가 생성한 증거 파일의 예시이며 현재 저장소에 생성되어 있다는 뜻이 아닙니다.

```sh
python3 tests/system/chat-external/oracle.py \
  --manifest /tmp/chat-run/manifest.json \
  --chat /tmp/chat-run/chat.json \
  --mock /tmp/chat-run/mock.json \
  --output /tmp/chat-run/result.json
```

출력은 본문·식별자·토큰 없이 판정, 문제 코드, 건수만 담습니다. `--output`은 새 파일만 만들고 기존 파일은 덮어쓰지 않습니다.
종료 코드는 `0=pass`, `1=failed`, `2=incomplete`입니다. `incomplete`도 통과가 아닙니다.

## 증거 형식 v1

세 파일 모두 다음 공통 필드를 정확히 가집니다. 알 수 없는 필드와 중복 JSON 필드는 거절합니다.

| 공통 필드 | 의미 |
| --- | --- |
| `schema_version: 1` | 증거 형식 버전입니다. |
| `run_id` | 세 파일에서 같은 합성 실행 ID입니다. |
| `synthetic: true` | 합성 데이터만 입력합니다. 원문·인증 정보·운영 자료는 금지합니다. |
| `complete: true` | 해당 run을 빠짐없이 수집한 종료 스냅샷이라는 수집기의 선언입니다. |

수집기는 페이지 일부나 진행 중 조회를 `complete: true`로 표시하면 안 됩니다. 주입 종료와 제한된 배출 관찰 후,
해당 run의 연결·업무 범위 전체를 관찰하고 세 스냅샷의 완료 경계를 맞춥니다. 현재 대조기는 선언의 진실을 스스로 입증하지 못합니다.

공통 `route` 필드는 `profile`, `connection_id`, `external_conversation_id`입니다.
`profile`은 `telegram`, `line`, `instagram`, `facebook`, `whatsapp`, `wechat`, `kakao-bizgo` 중 하나입니다.
UUID는 소문자 표준 문자열로 정규화합니다. `text_sha256`은 정규화나 trim 없이 원래 합성 본문의 UTF-8 바이트에 적용한 SHA-256 소문자 hex입니다.

| 파일/배열 | 각 행의 필드 |
| --- | --- |
| manifest / `inbound` | route + `external_message_id`, `external_sender_id`, `text_sha256` |
| manifest / `outbound` | route + `outbound_operation_id`, `text_sha256`, `expected_state`, `expected_effects` |
| manifest / `attempts` | `attempt_id`, `direction: inbound 또는 outbound`, `intent_index` |
| chat / `inbound_messages` | inbound 행 + `message_id`, `seq` |
| chat / `outbound_jobs` | outbound의 route·operation·hash + `state`, `effect_id` |
| mock / `effects` | outbound의 route·operation·hash + `effect_id` |

`intent_index`는 해당 방향 manifest 배열의 0부터 시작하는 인덱스입니다. `attempt_id`는 run에서 고유합니다.
동일 inbound 논리 키의 동일 입력 intent는 합치지만 시도는 별도 계수합니다. 같은 키의 다른 본문·화자는 잘못된 manifest로 거절합니다.
모든 고유 intent에는 시도가 최소 하나 있어야 합니다. 이 시도는 발생기의 업무 입력 시도이지 provider의 실제 효과 수가 아닙니다.
수집기는 재시도 시도마다 intent를 새 메시지로 바꾸지 않습니다.

inbound 논리 키는 `profile + connection_id + external_conversation_id + external_message_id`입니다.
`external_event_id`는 전달 시도 식별에 쓰는 wire 필드이므로 저장 메시지의 논리 키로 사용하지 않습니다.
발신은 `outbound_operation_id`를 키로 route와 hash까지 대조합니다.
`seq`는 양수 PostgreSQL bigint 범위의 십진 문자열이며 같은 방 안에서 중복될 수 없습니다.
외부 수신만 관찰하므로 사이에 다른 메시지가 있을 수 있으며 전체 방의 연속 seq 검증을 대체하지 않습니다.

## 결과 불명은 예상값을 명시합니다

| `expected_state` | `expected_effects` | Chat `effect_id` |
| --- | --- | --- |
| `accepted` | 정확히 1 | 실제 효과 원장의 effect ID와 일치합니다. |
| `rejected` | 정확히 0 | `null`입니다. |
| `unknown` | 시나리오가 지정한 0 또는 1 | `null`입니다. |

응답만 유실되어 Chat은 `unknown`이지만 mock에는 효과 1건이 있는 상황을 구분합니다.
unknown 선언이 누락·중복 효과를 허용하는 포괄 예외는 아닙니다. `pending`·`sending`은 종료 조건을 만족하지 못한 실패입니다.
Mock 전체 효과 원장은 시험 전용 증거이며 Chat 어댑터의 복구 API로 사용할 수 없습니다.

완전한 스냅샷에서 예상 메시지가 없으면 `failed`, 필수 파일·필드·완료 선언·시도 기록이 부족하면 `incomplete`입니다.
대조기의 통과는 입력된 범위의 데이터 대조 통과이며 UI·실제 플랫폼 호환성·성능 통과를 뜻하지 않습니다.

`test_oracle.py`의 합성 fixture와 negative control이 실행 가능한 형식 예시입니다.
누락·복제·목적지/본문/화자 변경, 계정별 같은 외부 ID, 결과 불명, 효과 ID 재사용, 부족한 증거를 검사합니다.

## 로컬 실행 설정 준비

`bootstrap.py`는 서버를 기동하지 않고 기존 `services/chat/.env`도 변경하지 않습니다.
저장소의 `/.artifacts/` ignore가 적용되어 있어야 하며 기존 run 폴더가 있으면 거절합니다.
다음 명령은 아직 사용하지 않은 합성 run ID로 실행합니다.

```sh
python3 tests/system/chat-external/bootstrap.py --prepare --run-id smoke-001
```

성공 시 stdout에는 아무것도 출력하지 않습니다. `.artifacts/chat-external/smoke-001/`을 mode700으로 만들고
다음 파일을 mode600으로 생성합니다. 내용을 출력하거나 shell 추적을 켠 상태로 로드하지 않습니다.

| 파일 | 적용 대상 |
| --- | --- |
| `chat.env` | Chat의 외부 실험 활성화·제어 토큰·발신 토큰·연결별 profile/토큰입니다. DB 설정은 기존 환경을 유지합니다. |
| `mock.env` | Mock의 제어·발신·연결별 토큰과 loopback URL입니다. |
| `harness.env` | 실행기의 run ID·연결 ID·제어 토큰·URL입니다. |

서로 다른 runtime의 env를 한 프로세스의 dotenv 파일로 합치지 않습니다.
배포 담당자가 각각의 설정을 해당 프로세스에 환경변수로 주입하고 기존 서비스 소유·포트 점유를 확인한 후 기동합니다.
이 도구에는 프로세스 시작·중지·migration·데이터 삭제 기능이 없습니다.

연결 UUID 산출식은 `uuid5(NAMESPACE_URL, "laughtale:external-smoke:" + run_id + ":" + profile)`입니다.
DB 연결 metadata의 `run_id`는 불변이므로 다른 run은 새 UUID와 credential 구성이 필요합니다.
여러 run을 같은 서버 설정으로 자동 병행하지 않습니다. 새로운 run 설정을 적용하려면 대상 프로세스 재기동을 별도로 관리합니다.

## T2a 수집기: 제한된 7개 프로필 왕복

실행 전에 migration·내부 합성 사용자 준비·Chat/mock 기동이 완료되어 있어야 합니다.
`harness.env` 내용을 해당 실행 프로세스의 환경변수로 안전하게 주입한 후 다음 명령을 실행합니다.
`--output-dir`은 존재하지 않는 새 하위 디렉터리여야 합니다.

```sh
python3 tests/system/chat-external/collect.py --execute \
  --run-id smoke-001 \
  --output-dir .artifacts/chat-external/smoke-001/evidence
```

환경변수는 `CHAT_CONTROL_TOKEN`, `MOCK_CONTROL_TOKEN`이 필수이며 `CHAT_BASE_URL`·`MOCK_BASE_URL`·
`CHAT_TEST_ORIGIN` 기본값은 각각 `http://127.0.0.1:18082`, `http://127.0.0.1:18087`, `http://127.0.0.1:18083`입니다.
`CHAT_TEST_CONNECTION_IDS`는 profile→UUID JSON이며 없으면 위 산출식을 사용합니다.
`harness.env`의 `CHAT_TEST_RUN_ID`는 launcher 참고값이며 CLI의 `--run-id`와 동일하게 사용합니다.
Mock의 service/connection bearer는 Mock과 Chat의 통신용입니다. 실행기는 이 토큰을 읽거나 전송하지 않습니다.

진행 순서는 readiness → 자체 합성 사용자 세션 → 7개 연결 seed → mock run 등록 →
프로필별 event 등록·1회 전달 → Chat history 확인·답장 → accepted 대기 → 전체 history·효과 원장 대조입니다.
새 연결에서 문의 7건·답장 7건만 순차 수행하며 기본 총 기한60초, HTTP 요청당 최대5초입니다.
자동 재시도·부하 증가·외부 URL·proxy·redirect는 지원하지 않습니다. 이 수치는 부하 성능 기준이 아닙니다.

`manifest.json`, `chat.json`, `mock.json`, `result.json`을 남깁니다. 원문은 저장하지 않고 각 관찰에서 독립적으로 hash를 계산합니다.
발신 operation ID는 서버가 할당하므로 입력의 client key로 확인한 ACK에서 식별자만 바인딩합니다.
예상 route와 본문 hash는 생성 입력이 소유하며 응답·효과 원장으로 덮어쓰지 않습니다.
Chat의 원본 external sender와 effect ID가 없으면 manifest 값으로 채워 넣지 않고 `incomplete`로 종료합니다.

HTTP200이어도 deliver 본문이 `rejected/403`이면 성공이 아닙니다. 인증 실패·잘못된 cursor·일부만 수집된 페이지·
예상 밖 상태는 통과로 바꾸지 않습니다. 실패 중 만들어진 seed·run은 자동 삭제하지 않으며 부분 증거를 남깁니다.
실제 UI·WebSocket 검증을 대체하지 않습니다.

## T2b 장애와 T2c 재시작

`collect.py --scenario`로 `normal`, `rate_limit`, `unavailable`, `reject`, `delay_after`, `unknown`, `duplicate`, `restart`를 선택합니다.
각 시나리오는 새 run ID와 그 run의 bootstrap 설정으로 수행합니다. 시나리오 사이에는 완전한 증거를 먼저 보존한 뒤
대상 Mock·Chat 설정을 갱신할 수 있지만, **restart 시나리오 도중에는 Mock을 재시작하지 않습니다.**

| 시나리오 | 주입과 기대 결과 |
| --- | --- |
| `rate_limit`, `unavailable` | operation 첫 요청에429·503을 주입하며 최종 accepted·효과1건입니다. |
| `reject` | 첫 요청403, 최종 rejected·효과0건입니다. |
| `delay_after` | 효과 후3초 지연, lookup 지원으로 accepted·효과1건입니다. |
| `unknown` | 같은 지연이지만 멱등·lookup 모두 미지원이며 unknown·효과1건입니다. |
| `duplicate` | 같은 수신을2개 thread로 동시에 요청합니다. 추가 본문 충돌409·다른 profile의 연결 사용403도 확인합니다. |
| `restart` | 효과 후10초 지연 중 첫 발신이 sending인 순간에 운영자 재시작을 기다립니다. |

`checks.json`은 부정 요청과 재시작 관찰을 기록합니다. duplicate의 manifest는 정상 논리 입력과 반복 시도를 계수하며,
고의로 본문·profile을 바꾼 부정 요청은 checks와 Mock 시도 원장으로 별도 검증합니다. 이 요청은 정상 메시지 intent가 아닙니다.
`transport-attempts.json`에는 원문·인증값을 제외한 실제 Mock 시도 원장을 남깁니다. `checks`의 actual inbound/outbound counts는
발생기 manifest의 입력 시도 수와 구분합니다. rate_limit/unavailable은 수신7·발신14회, 나머지는 발신7회이며 duplicate 수신은28회입니다.
fault별 실제 applied_fault7건을 확인하고, 지연은 elapsed_seconds도 확인합니다. 성공만 하고 장애가 주입되지 않은 시험은 통과하지 않습니다.

restart 실행 시 `evidence/pause-ready.json`이 생기면 Root가 **정확한 Chat 프로세스만** 종료·재기동합니다.
ready에는 관찰 시각·operation ID·sending 상태·이미 존재하는 mock 효과가 기록됩니다. 실제 종료 순간의 상태는
Root가 별도로 확인해야 하며, checkpoint 이후 작업이 이미 끝났다면 중간 처리 중 재시작을 증명했다고 주장하지 않습니다.
Chat이 다시 ready이면 같은 폴더에 `resume.json`을 `{"restart_confirmed":true}` 내용으로 생성합니다.
기존 신호 파일은 거절하며 대기는 최대120초입니다. 실행기는 이전 세션의401을 확인하고 새 합성 세션으로 로그인한 뒤
저장과 효과 대조를 계속합니다. 이전 세션이200이면 재시작 검증을 통과하지 않습니다. 실행기에 kill·자동 재시작 기능은 없습니다.
