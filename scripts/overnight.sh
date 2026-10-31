#!/usr/bin/env bash
# Overnight run: enqueue a batch, then start the supervisor detached (survives a closed terminal, keeps the Mac awake).
# usage: scripts/overnight.sh [YYYY-MM-DD] [local-spec]
set -euo pipefail
cd "$(dirname "$0")/.."
BATCH="${1:-$(date +%F)}"
LOCAL="${2:-screener:1,yf:1,docs_ar:4,docs_concall:2}"
mkdir -p logs reports
.venv/bin/python -m screener_scraper enqueue --batch "$BATCH"
caffeinate -dimsu nohup .venv/bin/python -m screener_scraper supervise --batch "$BATCH" \
  --local "$LOCAL" --dispatch-gh > "logs/supervisor_${BATCH}.log" 2>&1 &
echo "supervisor PID $! (batch $BATCH); log: logs/supervisor_${BATCH}.log; report: reports/${BATCH}.md"
