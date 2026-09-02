# Standalone image for the indicators dashboard + alert-watcher service.
# Build context MUST be the indicators/ directory itself (not the repo
# root) -- Dokploy's "build path" / "docker context path" setting should
# point at indicators/, keeping this service fully decoupled from app.py
# and every other file in trading-bot-executor. No Bybit trading key, no
# webhook secret, no Redis credential is ever copied into this image --
# this service only reads Bybit's PUBLIC market-data REST endpoints
# (klines, funding, open interest), which need no API key at all.
#
# DNS note: some fresh Docker hosts leave BuildKit's default bridge
# network for RUN steps unable to resolve DNS at all (host itself
# resolves fine -- systemd-resolved often binds a host-only loopback
# resolver like 127.0.0.53 that containers can't reach), so a plain
# `pip install` fails with "Temporary failure in name resolution"
# reaching PyPI. `RUN --network=host` would fix it but needs a BuildKit
# entitlement ("network.host is not allowed") that isn't granted here.
# Overwriting /etc/resolv.conf for just this layer needs no special
# privilege and works whether or not that root cause applies.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./indicators/requirements.txt
RUN echo "nameserver 8.8.8.8" > /etc/resolv.conf \
 && echo "nameserver 1.1.1.1" >> /etc/resolv.conf \
 && pip install --no-cache-dir -r indicators/requirements.txt

COPY . ./indicators/

ENV PYTHONUNBUFFERED=1

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "60", "indicators.web:app"]
