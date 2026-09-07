"""/api/version -- the one-curl answer to "which build is actually serving?"

Worth its own tests because the endpoint exists precisely to be trusted
during an incident: commit 47ed5eb once sat built-and-correct on GHCR for
three days while the container served the previous image, and the only
symptom was a missing line in Telegram messages. An endpoint that reported
a stale or guessed SHA would be worse than none at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicators.web import app  # noqa: E402

FIELDS = {"commit_sha", "built_at", "min_rr_ratio"}


@pytest.fixture
def client():
    return app.test_client()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test states its own environment; nothing leaks in from the shell
    that happens to be running pytest."""
    for var in ("GIT_COMMIT", "BUILD_TIME", "DASHBOARD_MIN_RR_RATIO"):
        monkeypatch.delenv(var, raising=False)


def test_reports_the_stamp_baked_in_at_build(client, monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "64f92e7deadbeef")
    monkeypatch.setenv("BUILD_TIME", "2026-09-07T09:00:00Z")
    body = client.get("/api/version").get_json()
    assert body["commit_sha"] == "64f92e7deadbeef"
    assert body["built_at"] == "2026-09-07T09:00:00Z"


def test_says_unknown_rather_than_guessing(client):
    """Outside a built image -- a local flask run, or `docker build` with no
    build-args -- there is no honest SHA to report. It must not fall back to
    reading the working tree's git state: that would describe the developer's
    checkout, not the running image, which is the exact confusion this
    endpoint exists to remove."""
    body = client.get("/api/version").get_json()
    assert body["commit_sha"] == "unknown"
    assert body["built_at"] == "unknown"


def test_reports_the_threshold_actually_in_effect(client, monkeypatch):
    """Read per request, so an environment override applied to the container
    shows up here. A value captured at import time would keep reporting the
    config.json default and quietly lie."""
    assert client.get("/api/version").get_json()["min_rr_ratio"] == 1.5
    monkeypatch.setenv("DASHBOARD_MIN_RR_RATIO", "2.75")
    assert client.get("/api/version").get_json()["min_rr_ratio"] == 2.75


def test_shape_is_exactly_the_three_documented_fields(client):
    """No paths, no data directory, no config dump: this route is
    unauthenticated like the rest of the app."""
    body = client.get("/api/version").get_json()
    assert set(body) == FIELDS


def test_is_never_cached(client):
    """A cached version response would report the previous build after a
    deploy -- the failure this endpoint is meant to detect."""
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"


def test_still_answers_when_the_config_cannot_be_read(client, monkeypatch):
    """A broken config must not take the SHA down with it -- during an
    incident the SHA is the part being asked for."""
    import indicators.web as web

    def boom():
        raise RuntimeError("config.json is malformed")

    monkeypatch.setattr(web, "load_config", boom)
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["commit_sha"] == "abc1234"
    assert body["min_rr_ratio"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
