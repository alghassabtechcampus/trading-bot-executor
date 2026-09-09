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

# Build stamp, so /api/version can say WHICH code is actually serving.
# Deliberately the last thing in the file: these change on every build, and
# anything below an invalidated layer rebuilds too -- down here they only
# ever invalidate themselves, not the pip install or the source copy.
# Values come from .github/workflows/indicators-image.yml; the defaults keep
# a plain local `docker build` working and honestly labelled "unknown".
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV GIT_COMMIT=$GIT_COMMIT
ENV BUILD_TIME=$BUILD_TIME
LABEL org.opencontainers.image.revision=$GIT_COMMIT \
      org.opencontainers.image.created=$BUILD_TIME \
      org.opencontainers.image.source=https://github.com/alghassabtechcampus/trading-bot-executor

EXPOSE 5050

# WORKER AVAILABILITY, not raw speed, is what takes this service down.
#
# One /api/check_alerts makes 45 sequential Bybit calls (9 symbols x 5
# distinct timeframes), measured at ~30s from a residential IP. With the
# unbounded 429 backoff this file's sibling sources/bybit_live.py used to
# carry, one rate-limited fetch could sleep 62s and a whole check could run
# for ~58 minutes. n8n calls every 15 minutes and there are 2 workers, so
# within half an hour BOTH were occupied and every later request simply
# queued forever -- including /api/version, which touches no network at all
# and still hung. That is the observed failure: not a slow endpoint, a
# service with no free worker.
#
# --timeout is therefore deliberately NOT generous. 120s is 4x the measured
# cost of a real check, so honest work finishes, while anything pathological
# is killed and its worker returned to the pool inside two minutes. Raising
# it further (300s was considered) would have made saturation last longer,
# not shorter. n8n's own 300s default now always fires AFTER this, so a
# failure arrives as a readable server-side error instead of a client-side
# abort -- which is exactly what left us guessing for two days.
#
# --access-logfile - puts every request and its duration in the container
# log, so next time the evidence is in Dokploy instead of being inferred.
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "indicators.web:app"]
