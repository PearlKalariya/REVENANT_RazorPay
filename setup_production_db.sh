#!/usr/bin/env bash
# Load the schema and seed data into the deployed (Neon) database.
#
# Reads NEON_DATABASE_URL from .env so the connection string — which carries a
# password — never reaches a shell history, a log, or a chat window.
#
# Safe to re-run: the schema is created fresh and the seed is idempotent.
set -euo pipefail
cd "$(dirname "$0")"

URL="$(grep -E '^NEON_DATABASE_URL=' .env | cut -d= -f2- || true)"
if [ -z "${URL}" ]; then
  echo "✗ NEON_DATABASE_URL is not set in .env"
  echo "  Add:  NEON_DATABASE_URL=postgresql://...?sslmode=require"
  exit 1
fi
case "$URL" in
  *sslmode=*) ;;
  *) URL="${URL}?sslmode=require"
     echo "▸ appended sslmode=require (Neon rejects unencrypted connections)" ;;
esac

echo "▸ Connecting"
docker run --rm postgres:16-alpine psql "$URL" -tAc "select version()" \
  | cut -c1-60 | sed 's/^/  /'

echo "▸ Loading schema"
docker run --rm -v "$PWD/db:/db:ro" postgres:16-alpine \
  psql "$URL" -v ON_ERROR_STOP=1 -q -f /db/schema.sql
docker run --rm postgres:16-alpine psql "$URL" -tAc \
  "select '  ' || count(*) || ' tables created' from information_schema.tables
    where table_schema='public'"

echo "▸ Seeding synthetic data"
DATABASE_URL="$URL" .venv/bin/python -m backend.seed | tail -8

echo "▸ Verifying"
docker run --rm postgres:16-alpine psql "$URL" -tAc \
  "select '  ' || (select count(*) from payments) || ' payments · '
       || (select count(*) from customers) || ' customers · '
       || (select count(*) from merchants) || ' merchant'"

echo
echo "✓ Production database ready."
echo "  Next: deploy the backend on Render with DATABASE_URL set to this string."
