"""github_dispatch.py and its call from refresh_main.py, offline.

A successful refresh sends one repository_dispatch so downstream jobs (the metric canary in
youtube-semantic-layer) run as soon as fresh data lands. The dispatch is best effort: it
must never change the refresh's own result, never log the token, and never fire from a run
that skipped, failed, or left days unwritten.
"""

import logging
from contextlib import nullcontext
from datetime import datetime, timezone

import requests

import github_dispatch
import refresh_main

TOKEN = "github_pat_SECRETSECRET"


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class Recorder:
    def __init__(self, response=None, exc=None):
        self.calls, self.response, self.exc = [], response or FakeResponse(204), exc

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.response


def log():
    return logging.LoggerAdapter(logging.getLogger("test"), extra={"run_id": "r1"})


def test_not_configured_sends_nothing(caplog):
    post = Recorder()
    with caplog.at_level(logging.INFO):
        assert github_dispatch.notify_refresh_complete("r1", log(), repo="", token="", post=post) is False
    assert post.calls == []
    assert any(github_dispatch.NOT_CONFIGURED_LOG in r.getMessage() for r in caplog.records)


def test_sends_the_documented_request():
    post = Recorder()
    assert github_dispatch.notify_refresh_complete("r1", log(), repo="owner/repo", token=TOKEN, post=post) is True
    url, kwargs = post.calls[0]
    assert url == "https://api.github.com/repos/owner/repo/dispatches"
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["json"] == {"event_type": "youtube-refresh-complete", "client_payload": {"refresh_run_id": "r1"}}
    assert kwargs["timeout"] == github_dispatch.TIMEOUT_SECONDS


def test_rejected_dispatch_warns_without_the_token(caplog):
    post = Recorder(FakeResponse(401, f"Bad credentials {TOKEN}"))
    with caplog.at_level(logging.INFO):
        assert github_dispatch.notify_refresh_complete("r1", log(), repo="owner/repo", token=TOKEN, post=post) is False
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(github_dispatch.FAILED_LOG) and "401" in m for m in messages)
    assert all(TOKEN not in m for m in messages)


def test_network_error_warns_without_the_token(caplog):
    post = Recorder(exc=requests.ConnectionError(f"boom {TOKEN}"))
    with caplog.at_level(logging.INFO):
        assert github_dispatch.notify_refresh_complete("r1", log(), repo="owner/repo", token=TOKEN, post=post) is False
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    assert any(r.getMessage().startswith(github_dispatch.FAILED_LOG) for r in caplog.records)


def _run_handler(monkeypatch, counts=None, refresh=None, lease=None):
    sent = []
    monkeypatch.setattr(refresh_main, "build_run_lease", lease or (lambda **kwargs: nullcontext()))
    monkeypatch.setattr(
        refresh_main, "run_refresh",
        refresh or (lambda run_date, refresh_run_id, log: {"outcomes": {}, "counts": counts or {}}),
    )
    monkeypatch.setattr(refresh_main, "notify_refresh_complete", lambda run_id, log: sent.append(run_id) or True)
    body, status = refresh_main.refresh_main(request=None)
    return body, status, sent


def test_clean_refresh_dispatches_once(monkeypatch):
    body, status, sent = _run_handler(monkeypatch, counts={"written": 30, "skipped_error": 0})
    assert status == 200 and sent == [body["run_id"]]


def test_refresh_with_failed_days_does_not_dispatch(monkeypatch):
    body, status, sent = _run_handler(monkeypatch, counts={"written": 28, "skipped_error": 2})
    assert status == 200 and sent == []


def test_failed_refresh_does_not_dispatch(monkeypatch):
    def boom(run_date, refresh_run_id, log):
        raise RuntimeError("down")
    body, status, sent = _run_handler(monkeypatch, refresh=boom)
    assert status == 500 and sent == []


def test_lease_held_does_not_dispatch(monkeypatch):
    class Held:
        def __enter__(self):
            raise refresh_main.LeaseHeld(type("R", (), {"owner": "daily", "expires_at": datetime(2026, 11, 8, tzinfo=timezone.utc)})())
        def __exit__(self, *a):
            return False
    body, status, sent = _run_handler(monkeypatch, lease=lambda **kwargs: Held())
    assert body.get("skipped") and sent == []


def test_dispatch_error_never_changes_the_refresh_result(monkeypatch):
    monkeypatch.setattr(refresh_main, "build_run_lease", lambda **kwargs: nullcontext())
    monkeypatch.setattr(refresh_main, "run_refresh",
                        lambda run_date, refresh_run_id, log: {"outcomes": {}, "counts": {"skipped_error": 0}})
    def explode(run_id, log):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(refresh_main, "notify_refresh_complete", explode)
    body, status = refresh_main.refresh_main(request=None)
    assert status == 200 and "error" not in body
