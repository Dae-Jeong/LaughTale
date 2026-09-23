# Local subscription registry

Redis는 메시지 저장소가 아니라 Gateway 주소·채팅방 구독의 TTL registry입니다.
메시지 원본은 PostgreSQL에, 비동기 전달 이벤트는 Kafka에 남깁니다.

최초 한 번 저장소 루트에서 실행합니다. 기존 자격 증명은 덮어쓰지 않습니다.

```bash
python3 infra/k8s/chat-external/prepare-realtime-secrets.py
docker compose -f infra/redis/compose.yaml config --quiet
docker compose -f infra/redis/compose.yaml up -d
```

Compose 네트워크 `laughtale-postgres-lab_default`가 먼저 있어야 합니다.
호스트 포트는 `127.0.0.1:19079`이며, Pod에서는 별도 Service의 `redis:6379`를 사용합니다.
Docker 주소가 달라지면 `../k8s/chat-external/distributed.yaml`의 EndpointSlice와 NetworkPolicy를 함께 수정합니다.

메모리는 128MiB, Redis 데이터 예산은 64MiB이고 `noeviction`입니다.
TTL 등록이 실패하면 Gateway가 연결을 정리하고 클라이언트는 Primary history로 복구합니다.
RDB/AOF는 사용하지 않습니다. 재시작 후 살아 있는 Gateway가 구독을 다시 등록합니다.

전용 실험 네트워크와 강한 `requirepass`를 사용하며 비밀번호는 무시된 `.artifacts/`에 0600으로 생성합니다.
현재는 default 사용자이고 TLS·역할별 ACL·HA는 적용하지 않았습니다. 운영 배포용 구성이 아닙니다.
인증을 포함한 전체 `docker compose config`나 Secret YAML을 출력하지 않습니다.

근거: [Redis 보안](https://redis.io/docs/latest/operate/oss_and_stack/management/security/),
[메모리 정책](https://redis.io/docs/latest/develop/reference/eviction/) — 2026-09-08 확인했습니다.
