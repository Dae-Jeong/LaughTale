# 로컬 Kafka 실험

내부 채팅 Outbox Relay의 실제 발행을 검증하는 단일 KRaft broker/controller입니다.
운영 클러스터가 아니며 RF1의 `acks=all`은 다중 broker 내구성을 보장하지 않습니다.
공용 인프라·외부 플랫폼 발신 job과 분리하며 Redis/Fanout은 아직 연결하지 않습니다.

Docker VM의 기존 메모리 제한 안에서 K8s 노드를 재시작하지 않기 위해 이번 절편은
Kafka를 전용 Compose로 실행합니다. API·Relay는 K8s, PostgreSQL도 별도 Compose입니다.
고정 ARM64 이미지와 상세 자원·보존 설정은 [compose.yaml](compose.yaml)이 소유합니다.
TLS/SASL 없는 합성 데이터 실험입니다. host listener는 loopback에만 공개하며 내부 listener는
전용 lab network에서만 사용합니다. 이 network의 컨테이너는 신뢰 경계 안에 있습니다.

## 실행

repo root에서 DB lab network가 있는지, 19092가 비어 있는지 먼저 확인합니다.

```sh
docker network inspect laughtale-postgres-lab_default
lsof -nP -iTCP:19092 -sTCP:LISTEN
docker compose -f infra/kafka/compose.yaml config --quiet
docker compose -f infra/kafka/compose.yaml up -d
docker compose -f infra/kafka/compose.yaml exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic chat.message-created.v1 --partitions 4 --replication-factor 1 --config min.insync.replicas=1 --config retention.ms=86400000 --config retention.bytes=268435456 --config segment.bytes=16777216
docker compose -f infra/kafka/compose.yaml exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --describe --topic chat.message-created.v1
```

K8s의 `kafka` selectorless Service/EndpointSlice는 `infra/k8s/chat-external/relay.yaml`에
있습니다. Kafka Docker IP와 NetworkPolicy의 `/32`가 일치하는지 매번 확인한 후 적용합니다.
브로커 advertised listener는 내부 `kafka:9092`, host verifier는 `127.0.0.1:19092`입니다.
Service port-forward만으로 host의 broker metadata 주소 문제가 해결된다고 가정하지 않습니다.

## 관측·중지

```sh
docker stats --no-stream laughtale-kafka-lab-kafka-1 k3d-laughtale-local-server-0
docker compose -f infra/kafka/compose.yaml exec -T kafka du -sk /var/lib/kafka/data
docker compose -f infra/kafka/compose.yaml stop kafka
```

`stop`은 데이터 volume을 보존합니다. `down -v`나 topic 삭제는 실행 안내에 포함하지 않습니다.
보존량은 partition별이며 active segment·metadata 때문에 hard disk quota가 아닙니다.
3GiB 사용·host 여유 10% 미만·K8s 노드 limit 85% 초과·OOM/재시작 시 부하를 중지합니다.
RF1이므로 Kafka volume 손실·24시간 보존 이후 자동 복구 보장은 없습니다. PostgreSQL 메시지 원장과
Outbox published는 영구 소비 보증이 아니며, 이후 replay/복구 정책을 별도로 검증해야 합니다.

설정 출처(2026-09-08): [공식 Docker 사용 안내](https://github.com/apache/kafka/blob/4.2.1/docker/examples/README.md),
[Kafka 4.2 topic 설정](https://kafka.apache.org/42/configuration/topic-configs/).
