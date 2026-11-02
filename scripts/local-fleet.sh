#!/bin/bash
# Local fleet: ONE process per source host, writing DIRECTLY to the local lake (Postgres :5433). Symbol lists come
# from Supabase TOP1000_MCAP (read-only). Usage: scripts/local-fleet.sh [start|stop|pause|resume|status]  (start is idempotent)
#   screener (screener.in, ranks 1000->101 desc) | yf (Yahoo) | bse (BSE annual reports) | nse (NSE-hosted ARs)
#   concalls (transcripts+ppt, any host, fallback chain) | sources (NSE/BSE archives: bhavcopy, deals, F&O)
# pause: each worker stops after its current company, and start (refresh.sh runs it) starts nothing until resume.
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
PY=.venv/bin/python
PAUSE=data/fleet/paused       # also read by fleet.py (PAUSE_FLAG) between companies
mkdir -p logs data/fleet
PROCS="screener:screener:0:1 yf:yf:0:1 bse:bse:0:1 nse:nse:0:1 concalls:concalls:0:1 sources:sources:0:1"

envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"' ; }
alive() { [ -f "data/fleet/$1.pid" ] && kill -0 "$(cat data/fleet/$1.pid)" 2>/dev/null; }

start_one() {
  local name=$1 role=$2 idx=$3 of=$4
  alive "$name" && return 0
  local src lake; src=$(envval DATABASE_URL); lake=$(envval LAKE_DATABASE_URL)
  [ -z "$src" ] && { echo "no DATABASE_URL in .env"; return 1; }
  [ -z "$lake" ] && lake="postgresql://localhost:5433/equity_lake"
  SOURCE_DATABASE_URL="$src" DATABASE_URL="$lake" SUPABASE_TXN_POOL=1 nohup caffeinate -dimsu bash -c '
    delay=20
    while [ ! -f '"$PAUSE"' ]; do
      start=$(date +%s)
      if [ "'"$role"'" = sources ]; then
        '"$PY"' -m screener_scraper sources --daily
        for _ in $(seq 60); do [ -f '"$PAUSE"' ] && break; sleep 30; done
      else
        '"$PY"' -m screener_scraper fleet-worker --role '"$role"' --index '"$idx"' --of '"$of"'
      fi
      [ -f '"$PAUSE"' ] && break
      ran=$(( $(date +%s) - start ))
      [ $ran -gt 300 ] && delay=20 || delay=$(( delay * 2 > 600 ? 600 : delay * 2 ))
      echo "[fleet '"$name"'] exited after ${ran}s; restarting in ${delay}s"
      sleep $delay
    done
    rm -f data/fleet/'"$name"'.pid
    echo "[fleet '"$name"'] paused"' >> "logs/fleet_$name.log" 2>&1 &
  echo $! > "data/fleet/$name.pid"
  # also into the worker's log: a fresh line there keeps unstick from taking a long pause for a hang
  echo "started $name ($role $idx/$of) pid $(cat data/fleet/$name.pid)" | tee -a "logs/fleet_$name.log"
}

# A healthy worker writes to its log at least every 30 min (longest sleep between passes). One silent for STALE_MIN
# is hung: dump its Python stacks into the log (SIGUSR1, see fleet.py), then kill it; its loop restarts it in ~20s.
STALE_MIN=${FLEET_STALE_MIN:-60}
unstick() {
  local name=$1 c
  alive "$name" || return 0
  [ -n "$(find "logs/fleet_$name.log" -mmin +$STALE_MIN 2>/dev/null)" ] || return 0
  for c in $(pgrep -P "$(cat "data/fleet/$name.pid")"); do
    case "$(ps -o comm= -p "$c" 2>/dev/null)" in
      *[Pp]ython*) kill -USR1 "$c" 2>/dev/null; sleep 2; kill "$c" 2>/dev/null
                   echo "[fleet $name] log silent ${STALE_MIN}+ min: stack dumped, restarted pid $c" | tee -a "logs/fleet_$name.log" ;;
    esac
  done
}

case "${1:-status}" in
  start)
    "$PY" -m screener_scraper lake-up >/dev/null 2>&1 || { echo "lake-up failed"; exit 1; }
    [ -f "$PAUSE" ] && { echo "fleet paused (scripts/local-fleet.sh resume)"; exit 0; }
    for p in $PROCS; do IFS=: read -r name role idx of <<<"$p"; start_one "$name" "$role" "$idx" "$of"; unstick "$name"; done ;;
  stop)
    for p in $PROCS; do name=${p%%:*}
      if alive "$name"; then pid=$(cat data/fleet/$name.pid); pkill -P "$pid" 2>/dev/null; pkill -P "$(pgrep -P "$pid" | head -1)" 2>/dev/null; kill "$pid" 2>/dev/null; fi
      rm -f "data/fleet/$name.pid"
    done; pkill -f "screener_scraper fleet-worker" 2>/dev/null; echo stopped ;;
  pause)
    touch "$PAUSE"; echo "pausing: each worker stops after its current company (scripts/local-fleet.sh resume)" ;;
  resume)
    rm -f "$PAUSE"; exec bash scripts/local-fleet.sh start ;;
  status)
    n=0
    for p in $PROCS; do name=${p%%:*}
      if alive "$name"; then s=up; n=$((n+1)); else s=DOWN; fi
      last=$(tail -n 1 "logs/fleet_$name.log" 2>/dev/null | cut -c1-110)
      printf '%-9s %-4s %s\n' "$name" "$s" "$last"
    done; echo "$n/6 running"
    if [ -f "$PAUSE" ]; then echo "paused since $(date -r "$PAUSE" '+%F %T') (scripts/local-fleet.sh resume)"; fi ;;
esac
