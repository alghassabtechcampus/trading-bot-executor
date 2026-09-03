# Standalone image for the indicators dashboard + alert-watcher service.
# Built by .github/workflows/indicators-image.yml on GitHub's runners (not
# on the Dokploy deploy target -- see that workflow's comment for why) and
# pushed to ghcr.io; Dokploy pulls the finished image rather than building
# from source. Build context is the indicators/ directory itself. No Bybit
# trading key, no webhook secret, no Redis credential is ever copied into
# this image -- this service only reads Bybit's PUBLIC market-data REST
# endpoints (klines, funding, open interest), which need no API key at all.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./indicators/requirements.txt
RUN pip install --no-cache-dir -r indicators/requirements.txt

COPY . ./indicators/

ENV PYTHONUNBUFFERED=1

# Runtime state lives OUTSIDE the image. Deploying replaces the container
# wholesale, so alert_state.json (the change-detection memory) and
# alert_log.jsonl (the permanent alert archive) would be wiped on every
# release if they stayed on the image filesystem -- and a wiped state file
# silently re-baselines every symbol, suppressing alerts for a cycle. Mount a
# named volume at /data in Dokploy to keep both across releases.
ENV INDICATORS_DATA_DIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "60", "indicators.web:app"]
