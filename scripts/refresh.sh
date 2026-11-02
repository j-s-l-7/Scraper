#!/bin/bash
# Autonomous refresh, run by launchd every 15 min (com.scraper.refresh).
#   1. pull extracted document text from finished GitHub `equity-docs` runs into data/documents/
#   2. incrementally re-index the document library (console search picks it up live)
#   3. refresh the batch report
#   4. push code changes to GitHub, but only if the full test suite passes and no secret is staged
# Create data/.no-autosync to disable step 4.
set -u
# Job control gives every command its own process group. launchd kills this job's process group when the script exits,
# which silently killed the detached lake-drive below (and any fleet worker this script restarted).
set -m
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
PY=.venv/bin/python
REPO=j-s-l-7/Scraper
mkdir -p logs data
LOG="logs/refresh_$(date +%F).log"
exec >>"$LOG" 2>&1
echo "=== refresh $(date '+%F %T') ==="

# Lock dirs (mkdir is atomic) hold their owner's pid and are stale only once that process is gone. The command line
# must match too, so a pid reused after a reboot doesn't count.
held() { local p; p=$(cat "$1/pid" 2>/dev/null) && ps -o command= -p "$p" 2>/dev/null | grep -q -- "$2"; }

# single instance
LOCK=data/.refresh.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  if held "$LOCK" refresh.sh; then echo "busy, skip"; exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# 1. collect finished docs runs not yet collected
DONE=data/.collected_runs; touch "$DONE"
for id in $(gh run list -R "$REPO" -w equity-docs.yml -s completed -L 20 --json databaseId --jq '.[].databaseId' 2>/dev/null); do
  grep -qx "$id" "$DONE" && continue
  echo "collect run $id"
  if "$PY" -m screener_scraper collect-documents "$id"; then echo "$id" >> "$DONE"; fi
done

# 1b. lake rollout driver (top 1000): load finished equity-lake runs, retry gaps once, dispatch the next window,
#     daily top-100 sync + equity-docs, weekly equity-shards. Idempotent; never fails; logs one summary line.
#     Detached with its own lock: artifact downloads can take hours on a slow link and must not hold up the rest.
#     Never two at once: load_run starts by wiping the run's download dir, which would destroy the other's downloads.
if mkdir data/.lake-drive.lock 2>/dev/null || { ! held data/.lake-drive.lock lake-drive &&
     rm -rf data/.lake-drive.lock && mkdir data/.lake-drive.lock; }; then
  nohup bash -c "LC_ALL=en_US.UTF-8 '$PY' -m screener_scraper lake-drive; rm -rf data/.lake-drive.lock" \
    >> "logs/lake_drive_$(date +%F).log" 2>&1 &
  echo $! > data/.lake-drive.lock/pid
else
  echo "lake-drive still running"
fi

# 1b2. local fleet: restart any dead worker process (idempotent; only starts the ones not running)
scripts/local-fleet.sh start 2>&1 | tail -n 3 || true

# 1c. data-quality engine: checks + bounded auto-remediation + scorecard; once per day per DB (marker in equity.control)
"$PY" -m screener_scraper dq --db both 2>&1 | tail -n 2 | cut -c1-400 || true

# 2. library index (incremental; skips docs whose file isn't local yet)
[ -d library ] && "$PY" -m library.index || echo "library index skipped/failed"

# 3. report for today's batch, if the supervisor tooling is present
"$PY" -m screener_scraper report --batch "$(date +%F)" >/dev/null 2>&1 || true

# 4. gated git autosync
[ -e data/.no-autosync ] && { echo "autosync disabled"; exit 0; }
if [ -n "$(git status --porcelain)" ]; then
  if "$PY" -m unittest discover -s tests -q >/dev/null 2>&1; then
    git add -A
    if git diff --cached | grep -qE 'sk-proj|sk-[A-Za-z0-9]{20,}|postgres(ql)?://[^:@ ]+:[^@ ]+@|eyJhbGci|sb_secret_'; then
      echo "secret-like string staged; autosync aborted"; git reset -q; exit 0
    fi
    git commit -q -m "chore(autosync): $(git diff --cached --name-only | wc -l | tr -d ' ') files, tests green

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
      && git pull -q --rebase origin main && git push -q origin main && echo "pushed $(git log --oneline -1)"
  else
    echo "tests failing; not pushing"
  fi
fi
