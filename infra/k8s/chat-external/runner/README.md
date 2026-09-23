# G4 machine API 발생기와 대사

Status: stdlib13개 단위 시험·Kustomize 렌더 통과, **이 하네스 담당자의 cluster 실행은 미수행**입니다.
외부 플랫폼 역할로 Chat Service에 합성 수신을 보냅니다. Mock 제어/답장 왕복은 기존 G1/G2/G3의 검증이며
이 runner는 다중 Pod 수신·중복·롤링 중 ACK 저장 보존에 집중합니다.

## 범위와 상한

기본7프로필70개 고유 이벤트와 동일 payload14개 replay, 초당5 HTTP 요청입니다.
설정 hard cap은초당10요청, 발생30초, concurrency4, 최대300시도이며 고유 이벤트600개 입력 상한보다
rate×duration×replay 상한이 먼저 적용됩니다. 요청 timeout2초, 응답16KiB, redirect/proxy 사용 금지입니다.
설정된 도착 시각과 실제 시작·완료·지연을 기록하며 밀린 요청을 한 번에 몰아서 발사하지 않습니다.
4개가 진행 중이면 추가 요청을 무한히 queue하지 않고 `skipped_at_capacity`로 기록합니다.

연결 seed7개는 부하 시작 전 별도 단계입니다. 전체 Job의 hard deadline60초이며 자동 Job 재실행은0회입니다.
실제 요청 본문·token·DB URL은 stdout 증거에 넣지 않습니다. request ID·합성 식별자·seq·시간·HTTP 결과만 남깁니다.
`run_id`는7개 연결에 동일하게 준비하고, `phase`를 `baseline`, `two-pods`, `rolling`처럼 구분합니다.
같은 phase를 재실행하면 같은 외부 event/message ID와 본문·발생시각을 사용합니다.
새 DB 데이터를 만들려면 phase를 바꾸며 무조건 UUID를 새로 만드는 재시도는 하지 않습니다.

## 실행 준비

1. `job.yaml`의 run ID를 이번 실험 전용으로 수정합니다. `chat-lab-env`의7개 연결 credential은 해당run용 UUID여야 합니다.
2. Chat의 합성 사용자 user_a가 DB에 준비돼 있어야 합니다. 다른 operator가 필요하면 `G4_OPERATOR_USER_ID`를 명시합니다.
3. Deployment1개 상태에서 seed·수신을 먼저 확인합니다. Browser cookie는 쓰지 않습니다.
4. generator에는 Secret의 `EXTERNAL_CONNECTION_CREDENTIALS`, `EXTERNAL_CONTROL_TOKEN`만 선택적으로 전달합니다. DB credential을 주지 않습니다.
5. `traffic-generator` Pod 라벨이 기존 NetworkPolicy의 허용 대상입니다. Secret을 가진 Pod가 다른 namespace/API로 나갈 수 있는지 negative check도 별도 수행합니다.

generator requests100m/64Mi·limits200m/96Mi를 더하면 Chat2+Mock+generator steady requests416Mi,
Chat rollout3+Mock+generator peak requests544Mi입니다. limits800Mi/1056Mi이며 실제 여유를 보장하지 않습니다.
다른 프로젝트를 중단하지 않고 압력이 생기면 이 시험을 중단합니다.

## Root가 수행할 명령

모두 repo root 기준입니다. 파일 생성·단위 시험만으로 아래 실험을 완료했다고 표시하지 않습니다.

```sh
python3 -m unittest discover -s infra/k8s/chat-external/runner -p 'test_*.py'
kubectl kustomize infra/k8s/chat-external/runner
kubectl --context k3d-laughtale-local apply -k infra/k8s/chat-external/runner
kubectl --context k3d-laughtale-local -n laughtale-chat-external wait --for=condition=complete job/g4-traffic --timeout=70s
python3 infra/k8s/chat-external/runner/collect.py --output .artifacts/chat-external/g4/baseline.json
```

Job 실패도 원인을 남깁니다. timeout이면 즉시 성공으로 처리하지 말고 Pod 상태·종료 코드와 로그를 확인합니다.
원장은 `/artifacts/result.json`과 Job stdout에 동일한 JSON으로 남습니다. 완료 컨테이너는 exec/cp할 수 없으므로
`collect.py`가 read-only `kubectl logs`로 수집합니다. 원장 없이 Job/Pod를 지우지 않습니다.

두 번째 실행 전 결과를 수집한 뒤 **해당 Job만** 삭제하고 phase를 바꿉니다. ConfigMap에 secret은 없습니다.

```sh
kubectl --context k3d-laughtale-local -n laughtale-chat-external delete job g4-traffic
```

- baseline: Chat1개에서7프로필/ACK/DB대사 확인
- two-pods: Chat2개로 scale한 뒤 동일 Service 대상에서 phase를 변경해 실행, request ID로 두 Pod 처리 확인
- rolling: Mock을 유지하고 Job 부하가 진행되는 동안 root가 Chat만 rollout restart, 이전·신규 Pod 로그를 수집
- HPA: 별도 HPA 적용 후 실제 HTTP 부하에서1→2 증가를 관찰합니다. 이 상한에서 증가하지 않으면 HPA 검증은 미완료입니다. CPU spinner·가짜 metric으로 성능 성공을 만들지 않습니다.

롤링 이전 Pod 로그는 삭제 후 `kubectl logs -l`로 얻을 수 없으므로 **시험 시작 전** root가 prefix를 붙인 로그 수집을
시작하고 새 Pod도 추가 수집합니다. Pod UID/기동·종료 시각·replica/HPA 상태도 같은 run에 기록합니다.

## DB·Pod 분포 대사

`database-proof.sql`은 root가 기존 전용 lab Primary에서 실행하는 read-only 쿼리입니다.
psql 변수 `run_id`, `phase`를 넘기면 본문·secret을 제외한 JSON 배열1개를 반환합니다.
DB row count만 비교하지 않고 각 ACK의 message ID·conversation ID·seq와 식별키별 중복까지 비교합니다.
계획 원장은 UTF-8 원문 `text_sha256`과 원래 채널·방·화자를 보존합니다. DB 쿼리도 같은 방식의 SHA256과
participant의 외부 화자 ID를 반환하여 ACK 유무와 관계없이 본문·라우팅·화자 변경을 검출합니다. 원문은 출력하지 않습니다.
`collect.py`는 기존 결과 파일을 덮어쓰지 않으며 새 경로를 요구합니다.

```sh
python3 infra/k8s/chat-external/runner/reconcile.py --result .artifacts/chat-external/g4/baseline.json --db-json .artifacts/chat-external/g4/baseline-db.json --pod-logs .artifacts/chat-external/g4/baseline-pods.log
```

Pod 로그는 `kubectl logs --prefix=true` 형식의 접두사와 JSON의 `app.work.id`를 사용합니다.
응답 `x-request-id`와 일치하는 실제 요청만 해당 Pod 처리로 셉니다. Ready Pod 수만으로 분산됐다고 추정하지 않습니다.
로그가 없으면 `pod_distribution_observed=0`, 매핑 불완전이면 false로 남습니다. 두 Pod 실험은 observed≥2와
해당시점의 Pod UID/replica 상태를 함께 확인합니다. 롤링 전체 Pod 수는 동시 replicas 수를 뜻하지 않습니다.

통과 기준:

- `acknowledged_but_missing_or_changed=0`
- `duplicate_db_messages=0`
- `source_content_or_route_mismatches=0`
- `planned_missing_in_db=0`, `unexpected_db_messages=0`
- 생성기 `conflicting_ack_identities=0`, `skipped_at_capacity=0`
- `execution_complete=true`: 독립 계획의 모든 attempt ID가 실제 시작·종료 원장과 일치하고, 예정된 duplicate도 모두 실행됐습니다.
- `observed_failed_attempts=0`: 실제 관측한 unknown/rejected/invalid response 등을 성공으로 숨기지 않습니다.
- 두 Pod 분포·롤링 상태 증거가 별도로 존재합니다.

`unknown` 응답 자체는 데이터 유실과 동일하지 않습니다. DB 대사에서 저장됐음을 확인할 수 있습니다.
이 경우 `acknowledged_but_missing_or_changed=0`과 HTTP 관측 실패를 별도로 보고하며, runner/대사 CLI의 전체 정상시험 종료 코드는 실패로 남습니다.
계획 원장이 없는 이전 형식의 결과나 재전송을 건너뛴 결과는 DB 행이 모두 있어도 정상 통과하지 않습니다.
반대로 Job 성공·ACK200만으로 저장 무손실이나 운영 무중단을 주장하지 않습니다.
이 runner는 쿠키 공유·사용자 WS fan-out·노드 장애 HA·실제 플랫폼의 처리량을 검증하지 않습니다.
