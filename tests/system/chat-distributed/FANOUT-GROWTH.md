# E3: Fanout 1 → 2 재할당 시험

한 합성 사용자·한 DM 방을 유지한 채 Fanout을 1개에서 2개로 늘립니다. Kafka 소비자 그룹의
실제 구성·파티션 소유권 변경·commit 진행과 두 Gateway의 메시지 수신을 함께 확인합니다.
한 hot partition의 처리량이나 수평 확장 성능을 증명하는 시험은 아닙니다.

## 실행

초기 구성은 API 2, Gateway 2, Fanout 1, Relay 1, proxy 1이며 기존 Pod는 모두 Ready·재시작 0입니다.
root가 baseline·포트 포워딩을 준비하고 아래 명령을 실행합니다. 시험기가 초기 2개를 1개로 축소하지 않습니다.

```bash
services/chat/.venv/bin/python tests/system/chat-distributed/fanout_growth.py --allow-fanout-scale --run-dir .artifacts/chat-distributed/fanout-e3-a
python3 -m unittest discover -s tests/system/chat-distributed -p 'test_fanout_growth.py'
```

발급은 API 직접 `18092`, 조회·전송은 고정 ingress `18096`, WS는 `18094/18095`입니다.
Kafka 관찰은 `127.0.0.1:19092`의 `chat-fanout-v1` 그룹과 `chat.message-created.v1` 토픽 4개 파티션만 사용합니다.
Fanout은 Downward API의 `LAB_POD_UID`로 `client_id=chat-fanout-<Pod UID>`를 노출해야 합니다.
익명 SDK client ID만 있으면 Pod와 소비자 참여를 연결할 수 없으므로 시험을 시작하지 않습니다.

| 시점 | 입력 |
| --- | --- |
| 0–10초 | 1건/초 · 10건 |
| 10–20초 | 5건/초 · 50건 |
| 20–35초 | 10건/초 · 150건 |

총 210건, WS 2개, 연결별 수신 증거·queue 256개, 전체 120초입니다.
최대 생성 지연은 1초, 실제 요청 간격은 해당 계획 간격+1초 이하여야 합니다. latency SLO가 아닙니다.
HTTP 결과 불명·거절·STOP·자원/관찰 오류는 실패이며 새 전송을 중단합니다.

t+10초에 고정 context `k3d-laughtale-local`·namespace `laughtale-chat-external`의
`deployment/chat-fanout`에만 `--replicas=2 --current-replicas=1 --resource-version=<직전 조회>` 조건으로
scale을 **한 번** 실행합니다. 권한·Deployment UID·직전 replica/Ready 상태를 먼저 확인합니다.
각 kubectl 명령은 5초 timeout이며 Ready 확인은 전송과 병렬로 진행합니다.
변경 결과 불명 시 재시도·자동 축소·롤백·데이터 삭제를 수행하지 않습니다.

## 독립 증거

`KafkaObserver`는 공개 Kafka Admin 조회와 group이 없는 manual-assigned Consumer의 offset 조회를 사용합니다.
관찰자가 Fanout group에 join하거나 offset commit하지 않습니다. 소비자 멤버십·소유권·commit/end 값은
순차 조회한 표본이며 하나의 원자적 snapshot이라고 주장하지 않습니다.

- `manifest.json`: 원문을 제외한 계획·hash·안전 상한입니다.
- `scale-request.json`, `scale-result.json`: 변경 시각·직전 상태·resourceVersion·결과입니다.
- `kafka.jsonl`: 약 1초 대기 간격의 멤버·Pod UID·파티션·commit·end·lag 관찰입니다.
- `resources.jsonl`: 기존 고정 Pod/컨테이너 정체, 새 Fanout UID 1개, 자원 임계치 검사입니다. 기존 E1 sampler를 변경하지 않습니다.
- `requests.jsonl`, `receipts.jsonl`: 합성 HTTP 요청과 WS별 실제 수신 증거입니다. 쿠키·본문은 기록하지 않습니다.
- `result.json`: 증설 전/재할당 중/두 소비자 Stable 이후 통계, lag 최대값, 최초·최종 assignment와 offset입니다.
- `final.json`: 기존 DB/outbox/Kafka verifier 입력입니다. root가 relay 완료 후 별도로 대조합니다.

처음과 마지막은 Stable·lag 0이어야 하며, 최종 표본은 마지막 요청 완료 이후여야 합니다.
처음 소비자 1개가 파티션 4개를 맡고, 최종 소비자 2개가 중복 없이 4개를 나누어 맡아야 합니다.
신규 member의 client ID는 실제 추가된 Fanout Pod UID와 일치해야 합니다.
commit 역행·누락·새 role 외 Pod 추가·기존 Pod 재시작·WS 누락/중복/본문 변경은 실패합니다.

새 소비자가 hot partition을 맡지 않을 수 있습니다. 그 경우에도 그룹 참여·재할당은 확인할 수 있으나
그 소비자가 이 방의 메시지를 처리했다고 표현하지 않습니다. `new_consumer_owns_hot_partition_at_end`
는 최종 할당 여부이며 처리량 증가 측정값이 아닙니다. 다중 방·다중 partition 활성화 시험은 후속 범위입니다.
