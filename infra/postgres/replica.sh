#!/bin/sh
set -eu

# Only this Compose service's named volume is initialized. Never erase partial data.
if [ "$(id -u)" = 0 ]; then
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"
    exec gosu postgres sh /opt/lab/replica.sh
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    # pg_basebackup refuses nonempty targets instead of deleting existing files.
    pg_basebackup -d 'host=primary port=5432 user=chat_replicator application_name=chat_replica connect_timeout=5' \
        -D "$PGDATA" -R -X stream -S chat_replica --no-password
fi

if [ ! -f "$PGDATA/standby.signal" ]; then
    echo 'Refusing to run replica without standby.signal; inspect the volume.' >&2
    exit 1
fi

exec postgres -D "$PGDATA" \
    -c hba_file=/etc/postgresql/lab_hba.conf \
    -c hot_standby=on -c shared_buffers="${REPLICA_SHARED_BUFFERS:-64MB}" \
    -c max_connections="${REPLICA_MAX_CONNECTIONS:-30}" \
    -c max_wal_senders=4 -c max_replication_slots=2
