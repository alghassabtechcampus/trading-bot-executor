# syntax=docker/dockerfile:1
#
# Standalone image for the indicators dashboard + alert-watcher service.
# Build context MUST be the indicators/ directory itself (not the repo
# root) -- Dokploy's "build path" / "docker context path" setting should
# point at indicators/, keeping this service fully decoupled from app.py
# and every other file in trading-bot-executor. No Bybit trading key, no
# webhook secret, no Redis credential is ever copied into this image --
# this service only reads Bybit's PUBLIC market-data REST endpoints
# (klines, funding, open interest), which need no API key at all.
#
# The `# syntax=` pragma above opts into BuildKit's extended Dockerfile
# frontend so `RUN --network=host` below is available. Some fresh
# Docker-host setups (systemd-resolved bound to a host-only loopback
# resolver, e.g. 127.0.0.53) leave the *default bridge* network BuildKit
# uses for RUN steps unable to resolve DNS at all, even though the host
# itself resolves fine -- pip install then fails with "Temporary failure
# in name resolution" reaching PyPI. Running just this one step on the
# host's own network namespace sidesteps that without touching the Docker
# daemon's config or needing host/SSH access.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./indicators/requirements.txt
RUN --network=host pip install --no-cache-dir -r indicators/requirements.txt

COPY . ./indicators/

ENV PYTHONUNBUFFERED=1

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "60", "indicators.web:app"]
