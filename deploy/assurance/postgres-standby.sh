#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
set -eu
mkdir -p "$PGDATA"
chown postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    # Refuse partial or unrelated data; recovery must be deliberate.
    if [ -n "$(ls -A "$PGDATA")" ]; then
        echo 'Standby directory is nonempty but uninitialized' >&2
        exit 1
    fi
    gosu postgres pg_basebackup --dbname="host=assurance-postgres user=nomos_replication application_name=assurance_standby" \
        --pgdata="$PGDATA" --wal-method=stream --slot=assurance_standby --write-recovery-conf
fi
exec docker-entrypoint.sh postgres -c hot_standby=on
