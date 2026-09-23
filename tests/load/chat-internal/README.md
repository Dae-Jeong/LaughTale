# 내부 DM 실측 부하

단일 Pod로 포트 포워딩한 `127.0.0.1:18092`에 실제 HTTP 요청을 전송합니다. 실행은 관리자가 CLI로 시작합니다. 대시보드는 읽기 전용이며 부하를 시작하거나 변경하지 않습니다.

```sh
uv tool run --from uv==0.12.10 uv run --project services/chat --locked python tests/load/chat-internal/run.py --run-dir .artifacts/chat-internal/새로운실행ID
python3 tests/load/chat-internal/dashboard.py --run-dir .artifacts/chat-internal/새로운실행ID --port 18091
```

대시보드는 `http://127.0.0.1:18091`입니다. 실행 전 별도 터미널에서 켜두면 상태 파일이 생성될 때 표시합니다. 기본 실험은 5/20/50/100 요청·초를 각 15초, 총 2,625건 전송 대상으로 계획합니다. `--stage-seconds 1`은 175건의 짧은 점검용입니다. 최대 4,000건/90초, 요청 예산 3초, 동시 요청 32개입니다. 대기열을 쌓지 않으며 포화 또는 0.5초 이상의 스케줄 지연은 드롭으로 기록합니다. 상한은 처리량 보장이 아닙니다.

API의 Host 검증 때문에 Host는 `127.0.0.1:18082`, Origin은 `http://127.0.0.1:18083`으로 유지합니다. 고정 개발 사용자 user_a와 고정 DM 방을 사전 확인합니다. 외부 주소·리다이렉트·환경 프록시는 허용하지 않습니다. 세션은 Pod 로컬이므로 Service 분산 포트 포워딩 대신 특정 Pod를 사용합니다.

실행 디렉터리에 `STOP` 파일을 생성하면 신규 전송을 중단하고 진행 중 요청 결과를 수집합니다. ACK 재시도는 없습니다. 5xx, 타임아웃, 잘못된 성공 응답은 저장 여부 불명으로 남기고 별도로 대사합니다. 응답 지연은 실제 시작부터 응답 읽기/계약 검증까지의 누적 분포이며 타임아웃도 포함합니다.

`manifest.json`, `plan.jsonl`, append-only `requests.jsonl`/`samples.jsonl`, `final.json`은 실행 증거입니다. 기존 실행 디렉터리는 덮어쓰지 않습니다. `live/status.json`만 원자적으로 교체하는 최신 표시용 상태입니다. 원문 메시지나 쿠키는 저장하지 않으며 합성 본문 SHA-256, 요청 키, ACK 메시지 ID/순번을 기록합니다. `complete`는 관측 종료 여부이며 `passed`는 모든 계획 요청의 정상 ACK 여부입니다. 둘 모두 DB/Kafka 대사를 대체하지 않습니다. 강제 프로세스 종료 시 final 파일이 없으면 완료로 간주하지 않습니다.

```sh
uv tool run --from uv==0.12.10 uv run --project services/chat --locked python -m unittest discover -s tests/load/chat-internal -p test_load.py
```
