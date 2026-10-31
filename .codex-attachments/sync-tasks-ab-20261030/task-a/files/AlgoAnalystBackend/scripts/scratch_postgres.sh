#!/usr/bin/env sh
# Start or stop a throwaway PostgreSQL cluster for the PostgreSQL-backed tests.
#
#   scripts/scratch_postgres.sh start   # initdb + start on a unix socket, prints the env to export
#   scripts/scratch_postgres.sh stop    # stop and delete the cluster
#
# Needs the PostgreSQL server binaries (initdb, pg_ctl) on PATH. Nothing here
# touches any other database: the cluster lives in $SCRATCH_PG_DIR and listens
# only on a unix socket in $SCRATCH_PG_SOCKET_DIR.
set -eu
SCRATCH_PG_DIR="${SCRATCH_PG_DIR:-/tmp/algoanalyst-scratch-pg}"
SCRATCH_PG_SOCKET_DIR="${SCRATCH_PG_SOCKET_DIR:-/tmp}"
SCRATCH_PG_PORT="${SCRATCH_PG_PORT:-54329}"

case "${1:-}" in
  start)
    if [ ! -f "$SCRATCH_PG_DIR/PG_VERSION" ]; then
      initdb -D "$SCRATCH_PG_DIR" -U postgres --auth=trust -E UTF8 >"$SCRATCH_PG_DIR.initdb.log" 2>&1
    fi
    pg_ctl -D "$SCRATCH_PG_DIR" -l "$SCRATCH_PG_DIR.log" \
      -o "-p $SCRATCH_PG_PORT -k $SCRATCH_PG_SOCKET_DIR -c listen_addresses=''" start >/dev/null
    echo "export ALGOANALYST_TEST_PG_HOST=$SCRATCH_PG_SOCKET_DIR ALGOANALYST_TEST_PG_PORT=$SCRATCH_PG_PORT ALGOANALYST_TEST_PG_USER=postgres"
    ;;
  stop)
    pg_ctl -D "$SCRATCH_PG_DIR" -m fast stop >/dev/null 2>&1 || true
    rm -rf "$SCRATCH_PG_DIR" "$SCRATCH_PG_DIR.log" "$SCRATCH_PG_DIR.initdb.log"
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 2
    ;;
esac
