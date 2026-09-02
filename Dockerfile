# REVENANT backend.
#
# Single stage: the dependency set is small and the image is short-lived on a
# free tier. A multi-stage build would save ~80 MB and cost more than it saves
# in build minutes.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY db/ ./db/

# Run unprivileged: nothing here needs root, and a container that can write to
# its own system files is a container an exploit can persist in.
RUN useradd --create-home --uid 10001 revenant && chown -R revenant:revenant /app
USER revenant

# Hosts inject $PORT and expect the app to honour it.
ENV PORT=8000
EXPOSE 8000

# Two requirements that pull against each other, both satisfied here:
#
#   $PORT must expand — hosts (Render, Koyeb, Fly) inject the port they route
#   to, and uvicorn does NOT read it from the environment. Bare exec form left
#   it listening on 8000 while the platform expected 8099: a health check that
#   never passes and a deploy that looks broken for no visible reason.
#
#   uvicorn must be PID 1 — otherwise /bin/sh holds that slot, swallows
#   SIGTERM, and the platform kills the container instead of letting it drain,
#   dropping in-flight requests on every deploy.
#
# `exec` inside the shell form gives both: the shell expands ${PORT}, then
# replaces itself with uvicorn.
#
# One worker: free tiers give a fraction of a CPU, and a second would contend
# for it while doubling the memory.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
