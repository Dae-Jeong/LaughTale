FROM postgres:16-alpine@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685
COPY infra/postgres/init.sql /docker-entrypoint-initdb.d/10-chat.sql
COPY infra/postgres/pg_hba.conf /etc/postgresql/lab_hba.conf
COPY infra/postgres/replica.sh /opt/lab/replica.sh
