# Standalone image for the indicators dashboard + alert-watcher service.
# Build context MUST be the indicators/ directory itself (not the repo
# root) -- Dokploy's "build path" / "docker context path" setting should
# point at indicators/, keeping this service fully decoupled from app.py
# and every other file in trading-bot-executor. No Bybit trading key, no
# webhook secret, no Redis credential is ever copied into this image --
# this service only reads Bybit's PUBLIC market-data REST endpoints
# (klines, funding, open interest), which need no API key at all.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./indicators/requirements.txt
RUN pip install --no-cache-dir -r indicators/requirements.txt

COPY . ./indicators/

ENV PYTHONUNBUFFERED=1

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "60", "indicators.web:app"]
