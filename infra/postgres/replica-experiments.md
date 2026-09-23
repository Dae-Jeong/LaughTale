# E4 · Replica 읽기와 물리 확장 실험

Status: 실행 준비안입니다. root가 실행하며 제품 API는 계속 Primary를 사용합니다.

## E4a · 기존 Replica의 안전한 읽기

실험 전용 `tests/system/chat-distributed/replica_history.py`는 각 endpoint에 reader 연결 하나만 사용합니다.
Primary에서 매 페이지 membership을 확인합니다. 고정 head를 읽은 **뒤** WAL insert LSN을 가져오며,
같은 system identifier/timeline의 Replica가 그 위치까지 replay한 경우에만 본문을 읽습니다.
gate 이후 새 READ COMMITTED read-only transaction을 열므로 gate 이전의 오래된 MVCC snapshot을 재사용하지 않습니다.
현재 append-only/삭제 없는 DM 계약 전용이며 메시지 수정·삭제·샤딩·승격으로 확대 적용하지 않습니다.

Replica가 늦거나 관측에 실패하면 Replica 본문 조회를 하지 않습니다. 연속성 검증에 실패한 Replica 결과도
반환하지 않습니다. Primary fallback은 실행당 최대 10페이지·동시 1개·본문 쿼리 2초이며,
예산 소진 시 `incomplete`로 끝냅니다. snapshot 최대 100건, 페이지 5건, 전체 실행 예산 120초입니다.
원장은 ID·seq·본문 SHA-256만 저장하고 원문·자격증명을 출력하지 않습니다. 기존 증거 파일을 덮어쓰지 않습니다.

root 실행 순서는 다음과 같습니다.

1. 기존 컨테이너 label/port와 Primary/Replica system identifier·timeline을 확인합니다.
2. 기존 Replica에서 `pg_wal_replay_pause()`를 호출하고 실제 pause 상태를 확인합니다. 이미 pause 상태였다면 임의로 상태를 덮지 않습니다.
3. 승인된 기존 HTTP 경로로 합성 메시지를 추가한 뒤 snapshot을 만듭니다. pause 전에 만들었던 snapshot만으로 lag를 검증하지 않습니다.
4. 동일 snapshot을 `--expect-route primary_fallback`으로 조회합니다. `--fallback-limit 0` 대조군은 정상 성공으로 끝나면 안 됩니다.
5. **root 제어기의 finally에서 `pg_wal_replay_resume()`를 호출합니다.** 중간 명령 실패·취소에도 복구하고, resume 후 실제 replay 진행을 확인합니다.
6. 같은 snapshot을 `--expect-route replica`로 조회하여 Primary reference의 ID/seq/hash와 전부 대사합니다.

```sh
uv tool run --from uv==0.12.10 uv run --project services/chat --locked python tests/system/chat-distributed/replica_history.py snapshot --actor-id <UUID> --conversation-id <UUID> --window 20 --output .artifacts/chat-distributed/<run>/snapshot.json
uv tool run --from uv==0.12.10 uv run --project services/chat --locked python tests/system/chat-distributed/replica_history.py verify --snapshot .artifacts/chat-distributed/<run>/snapshot.json --expect-route primary_fallback --output .artifacts/chat-distributed/<run>/paused.json
uv tool run --from uv==0.12.10 uv run --project services/chat --locked python tests/system/chat-distributed/replica_history.py verify --snapshot .artifacts/chat-distributed/<run>/snapshot.json --expect-route replica --output .artifacts/chat-distributed/<run>/caught-up.json
```

앱 쿠키 인증을 이 CLI가 구현하지는 않습니다. 관리자가 지정한 합성 actor/room의 Primary membership을 검사하는 읽기 경로 실험입니다.
기존 `verify.py`가 확인한 reader 쓰기 거절(42501)·Replica writer 쓰기 거절(25006)은 별도 권한 증거이며,
read-only transaction 성공만으로 reader ACL을 검증했다고 주장하지 않습니다.

## E4b · 신규 빈 Replica 추가

현재 물리 구성은 Primary+Replica **2개에서 3개로 증가**합니다. 기존 5441 복제본과 볼륨은 보존합니다.
기존 Replica를 중지하더라도 데이터를 보유한 물리 인스턴스 자체가 사라지는 것은 아닙니다.

root만 실행합니다. `compose.yaml`에 `replica-candidate.compose.yaml`을 추가하고 `--profile replica-growth`로
`replica-candidate` 하나만 선택합니다. 기존 service 전체 `up`/재생성이나 `down -v`는 하지 않습니다.
5442의 실제 점유 확인, 새 `replica-candidate-data` 볼륨의 기존 여부 확인, Primary replication slot 여유 확인이 선행합니다.
기존 slot은 재사용하지 않으며 `chat_replica_candidate` 슬롯 생성은 root가 존재 여부 확인 후 별도로 수행합니다.

후보 자원은 256MiB/0.5 CPU, 데이터 복사 5MiB/s, spread checkpoint입니다. `pg_basebackup`은 120초 후 TERM,
5초 뒤에도 종료하지 않으면 KILL하며 부분 데이터는 그대로 보존합니다. 기존·실패 볼륨의 자동 삭제/재초기화는 없습니다.
120초 내 복사/catch-up이 안 끝나면 미완료입니다. 제한을 조용히 늘리지 않습니다.

디스크 guard 후보: 신규 볼륨 1GiB, 새 slot WAL 보관 64MiB, Primary WAL의 기준선 대비 증가 128MiB,
호스트 free 20% 미만이면 신규 작업을 중단합니다. root가 5초마다 기록합니다. 이는 관측/중단 임계치이며
filesystem hard quota는 아닙니다. `max_slot_wal_keep_size=256MB` 역시 전체 디스크의 엄격한 상한이 아닙니다.
실패 후 비활성 slot을 방치하면 WAL이 쌓일 수 있습니다. root는 신규 candidate가 중지됐음을 확인한 뒤
**신규 slot만** 별도로 제거할지 판단합니다. 볼륨·기존 slot·기존 Replica는 자동 정리하지 않습니다.

Replica health는 recovery 여부일 뿐 편입 준비 완료가 아닙니다. 복사 완료→streaming→system/timeline 일치→
snapshot watermark replay→메시지 대사 순서를 통과한 후에만 실험 읽기 대상으로 사용합니다.
5442 후보는 `verify --replica-port 5442`로 선택합니다. 고정 container/service label과 정확한 loopback 포트,
system identifier/timeline을 다시 확인하며 임의 호스트·포트는 허용하지 않습니다.

근거: [PostgreSQL WAL 위치와 replay 함수](https://www.postgresql.org/docs/16/functions-admin.html),
[Hot Standby](https://www.postgresql.org/docs/16/hot-standby.html),
[pg_basebackup 제한과 옵션](https://www.postgresql.org/docs/16/app-pgbasebackup.html). 확인일: 2026-09-08.
