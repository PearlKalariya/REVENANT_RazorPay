#!/usr/bin/env bash
# Bring the whole stack up. Safe to re-run.
#
# Docker Desktop stops when the machine sleeps, which takes Postgres with it —
# and then the backend 500s, the tests error, and the dashboard shows nothing.
# One command avoids diagnosing that chain again.
set -euo pipefail
cd "$(dirname "$0")"

echo "▸ Docker"
docker info >/dev/null 2>&1 || {
  open -a Docker
  for _ in $(seq 1 40); do docker info >/dev/null 2>&1 && break; sleep 3; done
}

echo "▸ Postgres"
docker compose up -d db >/dev/null
for _ in $(seq 1 30); do
  [ "$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q db)" 2>/dev/null)" = healthy ] && break
  sleep 2
done
docker compose exec -T db psql -U revive -d revive -tAc \
  "SELECT '  ' || count(*) || ' payments · ' ||
   (SELECT count(*) FROM recovery_actions) || ' actions · ' ||
   (SELECT count(*) FROM recovery_outcomes) || ' outcomes' FROM payments"

echo "▸ Backend"
pkill -f "uvicorn backend.main" 2>/dev/null || true
sleep 1
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning \
  > /tmp/revenant_api.log 2>&1 &
for _ in $(seq 1 20); do
  curl -sf -o /dev/null http://127.0.0.1:8000/health && break
  sleep 1
done
echo "  http://127.0.0.1:8000"

echo "▸ Credentials"
.venv/bin/python -m backend.verify_credentials 2>/dev/null | grep -E "PASS|FAIL|SKIP|ready" | sed 's/^/ /'

echo "▸ Dashboard"
echo "  starting Next on :3000 — Ctrl-C stops everything"
cd frontend && exec npm run dev
