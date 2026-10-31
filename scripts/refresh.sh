#!/bin/bash
# Autonomous refresh, run by launchd every 15 min (com.scraper.refresh).
#   1. pull extracted document text from finished GitHub `equity-docs` runs into data/documents/
#   2. incrementally re-index the document library (console search picks it up live)
#   3. refresh the batch report
#   4. push code changes to GitHub, but only if the full test suite passes and no secret is staged
# Create data/.no-autosync to disable step 4.
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
PY=.venv/bin/python
REPO=j-s-l-7/Scraper
mkdir -p logs data
LOG="logs/refresh_$(date +%F).log"
exec >>"$LOG" 2>&1
echo "=== refresh $(date '+%F %T') ==="

# single instance (mkdir is atomic; stale after 2h)
LOCK=data/.refresh.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +120)" ]; then rm -rf "$LOCK"; mkdir "$LOCK"; else echo "busy, skip"; exit 0; fi
fi
trap 'rm -rf "$LOCK"' EXIT

# 1. collect finished docs runs not yet collected
DONE=data/.collected_runs; touch "$DONE"
for id in $(gh run list -R "$REPO" -w equity-docs.yml -s completed -L 20 --json databaseId --jq '.[].databaseId' 2>/dev/null); do
  grep -qx "$id" "$DONE" && continue
  echo "collect run $id"
  if "$PY" -m screener_scraper collect-documents "$id"; then echo "$id" >> "$DONE"; fi
done

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
    AUTHOR_NAME=$(git log -1 --format=%an 182ce77); AUTHOR_EMAIL=$(git log -1 --format=%ae 182ce77)
    git -c user.name="$AUTHOR_NAME" -c user.email="$AUTHOR_EMAIL" commit -q -m "chore(autosync): $(git diff --cached --name-only | wc -l | tr -d ' ') files, tests green

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
      && git pull -q --rebase origin main && git push -q origin main && echo "pushed $(git log --oneline -1)"
  else
    echo "tests failing; not pushing"
  fi
fi
