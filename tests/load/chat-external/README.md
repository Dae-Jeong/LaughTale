# 외부 수신 부하 실험

별도 프로세스에서 합성 입력만 생성하는 표준 라이브러리 도구입니다. Chat/mock/DB/K8s를 기동·정지·변경하지 않습니다.
시나리오 실행은 운영자가 대상과 자원 상태를 확인한 뒤 수행합니다.

## 발생기 기준 시험

포트18089가 비어 있음을 확인한 뒤 운영자가 별도 터미널에서 sink를 기동합니다. 인증값·DB·Chat을 사용하지 않는 HTTP ACK 대역입니다.

```sh
python3 tests/load/chat-external/sink.py --port 18089
python3 tests/load/chat-external/load.py --execute --sink-only \
  --run-id generator-baseline --seconds 30 \
  --output-dir .artifacts/chat-external/generator-baseline
```

`generator_baseline=pass`는 예정량을 보내고 전체 lag p95≤100ms인 이 발생기의 기준 시험입니다.
Chat의 성능·정합성 통과가 아니며, 실제 mock 경로의 비용까지 검증하지 않습니다.

## Chat 경로 시험

[시스템 시험 bootstrap](../../system/chat-external/README.md)으로 새 run의 설정을 준비하고 각 runtime에 적용합니다.
`harness.env`가 실행기 환경변수로 주입되어 있어야 합니다. 기존 run·연결을 다른 run ID에 재사용하지 않습니다.

```sh
python3 tests/load/chat-external/load.py --execute \
  --run-id load-distributed-001 --seconds 30 --distribution distributed \
  --output-dir .artifacts/chat-external/load-distributed-001/evidence
```

`--distribution hot`은 같은 총량의80%를 한 방에 집중합니다. 독립 run으로 두 분포를 비교합니다.
현재는 외부 수신 부하이며 답장·WS fan-out 부하는 포함하지 않습니다.

| 경계 | 값 |
| --- | --- |
| 채널·방 | 7개 프로필·각2개 방 |
| 요청률 | 1→5→10개 고유 수신/초 |
| 시간 | rate별1~30초, 기본30초, 최대480개 intent·600개 보호 상한 |
| 발생 | 예정 시각 기반 open-loop, 최대8개 미완료 요청, 무한 대기열 없음 |
| 본문 | 합성256바이트 ASCII 본문 |
| 준비 | 이벤트 등록은 측정 시작 전에 수행하며 Chat 요청 처리량에 포함하지 않습니다. |
| 중단 | 오류·발생기 동시성 상한·lag p95>100ms가10초 지속·오래된 미완료30초·STOP 파일 |

Root가 호스트 메모리 압박·OOM·공유 서비스 영향을 관찰하면 출력 폴더에 `STOP` 파일을 생성해 신규 주입을 중단할 수 있습니다.
이미 시작한 요청은 최대 HTTP5초 범위로 회수하며 이후 대조 수집은 최대30초입니다.
발생기가 막혀 덜 보낸 결과는 Chat의 처리량 상한으로 해석하지 않습니다. 프로세스별 CPU/RSS·DB 잠금/풀·Mock 자원·
event-loop 지연 등의 관측은 별도로 함께 수집해야 합니다. 아래 보조 샘플러가 현재 존재하는 지표를 수집하며 운영 성능 SLO는 이 도구에 없습니다.

`timing.json`은 예정/실제 시작/완료·lag·HTTP 왕복 지연·오류를, `summary.json`은 rate별 표본 수·p95/p99를 담습니다.
본문·토큰은 남기지 않습니다. 적은 표본의 percentile은 참고값입니다. `correctness=pass`와 `performance_slo=not_evaluated`를 분리합니다.
이 지연은 발생기→Mock→Chat→Mock 응답 경로이며 순수 Chat 처리 시간이라고 설명하지 않습니다.

완료 후 14개 방의 전체 history와 Mock의 전체 원장을 읽어 입력 manifest와 독립 대조합니다.
`manifest/chat/mock/result.json`이 근거이며 발생 누락·중도 정지는 `incomplete`입니다. 예상하지 않은 provider 효과도 검출합니다.
수집기의 순서 확인은 실제 저장 seq 기준이며 네트워크 도착이나 원래 발생 시각의 전역 순서를 주장하지 않습니다.

## 도구 자체 검증

```sh
python3 -m unittest discover -s tests/load/chat-external -p 'test_*.py' -v
```

기본 시험은 가상 시계·대역 응답을 사용하며 실제 sink·Chat·Mock에 요청하지 않습니다.

## 로컬 자원 보조 샘플러

부하 실행기가 새 evidence 폴더를 만든 직후, Root가 별도 프로세스로 연결합니다. 샘플러가 먼저 폴더를 예약하지 않습니다.

```sh
python3 tests/load/chat-external/resources.py --seconds 100 \
  --output-dir .artifacts/chat-external/load-distributed-001/evidence
```

최대100초 동안5초 간격으로18082·18087의 실제 listener PID와 `ps`의 CPU·RSS, 지정한 lab Primary/Replica의
Docker stats·OOM/재시작 상태, 호스트 free memory%·swap, Chat `/metrics`를 읽습니다. 프로세스 args·env·stderr는 기록하지 않습니다.
명령과 HTTP에는 짧은 기한이 있으며 오류는 `resources.json`의 `incomplete`와 sample별 코드로 남깁니다.
free memory<10%, 대상 실종/교체·OOM·재시작은 같은 폴더의 STOP 파일로 신규 부하 중단을 요청합니다. 서비스 프로세스를 종료하지 않습니다.

`metrics-NNN.prom`은 현재 실제 존재하는 DB pool/transaction·chat_ws 계열과 안전한 enum/bucket label만 보존합니다.
HTTP route·임의 label·HELP는 민감값을 피하기 위해 제외하며 제외 건수와 수집 범위를 JSON에 명시합니다.
event-loop 지표의 존재 여부는 기록하지만, 지표가 없으면 측정했다고 주장하지 않습니다. Metrics API가 없거나 필수 수집이 실패하면 통과로 숨기지 않습니다.
