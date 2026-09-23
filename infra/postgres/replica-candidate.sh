#!/bin/sh
set -eu

if [ "$(id -u)" = 0 ]; then
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"
    exec gosu postgres sh /opt/lab/replica-candidate.sh
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    # 기존 파일이나 실패한 복사 흔적은 삭제하지 않습니다.
    timeout -s TERM -k 5 120 pg_basebackup \
        -d 'host=primary port=5432 user=chat_replicator application_name=chat_replica_candidate connect_timeout=5' \
        -D "$PGDATA" -R -X stream -S chat_replica_candidate \
        --checkpoint=spread --max-rate=5M --no-password
fi

if [ ! -f "$PGDATA/standby.signal" ]; then
    echo 'Candidate standby is incomplete; preserve volume and inspect.' >&2
    exit 1
fi

exec postgres -D "$PGDATA" \
    -c hba_file=/etc/postgresql/lab_hba.conf \
    -c hot_standby=on -c shared_buffers=64MB -c max_connections=30 \
    -c max_wal_senders=4 -c max_replication_slots=2
