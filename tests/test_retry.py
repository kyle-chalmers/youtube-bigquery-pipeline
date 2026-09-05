"""The retry policy is shared by every API client. Pin it down.

Retrying only 429 once dropped a video's traffic on the first transient 500, so the
retryable set is {429, 500, 502, 503, 504}. Callers choose their own attempt count:
the Cloud Function uses 3, the backfill uses 5, and neither may silently inherit the
other's value. Transport exceptions are deliberately NOT retried today (pre-existing
behaviour); widening that is a Phase 4 decision, so the current behaviour is pinned.
"""

import socket

import httplib2
import pytest
import requests
from googleapiclient.errors import HttpError

import backfill_analytics
import youtube_analytics_api as ya
from retry import RETRYABLE_STATUSES, http_status_of, with_retry


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"boom")


def requests_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("retry.time.sleep", lambda s: slept.append(s))
    return slept


def flaky(exceptions):
    """A callable that raises each exception in turn, then returns 'ok'."""
    calls = {"n": 0}
    queue = list(exceptions)

    def fn():
        calls["n"] += 1
        if queue:
            raise queue.pop(0)
        return "ok"

    fn.calls = calls
    return fn


def test_retryable_set_is_exactly_the_five():
    assert RETRYABLE_STATUSES == frozenset({429, 500, 502, 503, 504})
    assert ya.YouTubeAnalyticsAPI.RETRYABLE_STATUSES is RETRYABLE_STATUSES


def test_status_extraction_covers_both_client_libraries():
    assert http_status_of(http_error(503)) == 503
    assert http_status_of(requests_error(429)) == 429
    assert http_status_of(socket.timeout()) is None
    assert http_status_of(ValueError("x")) is None


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_retries_each_transient_googleapiclient_status(status, no_sleep):
    fn = flaky([http_error(status)])
    assert with_retry(fn, max_retries=3) == "ok"
    assert fn.calls["n"] == 2
    assert no_sleep == [1]


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_retries_each_transient_requests_status(status):
    fn = flaky([requests_error(status)])
    assert with_retry(fn, max_retries=3) == "ok"
    assert fn.calls["n"] == 2


def test_backs_off_exponentially(no_sleep):
    fn = flaky([http_error(500), http_error(502), http_error(503)])
    assert with_retry(fn, max_retries=3) == "ok"
    assert no_sleep == [1, 2, 4]


def test_gives_up_after_cap():
    fn = flaky([http_error(503)] * 4)
    with pytest.raises(HttpError):
        with_retry(fn, max_retries=3)
    assert fn.calls["n"] == 4  # initial + 3 retries


@pytest.mark.parametrize("exc", [http_error(403), http_error(400), requests_error(404)])
def test_does_not_retry_non_transient_status(exc):
    fn = flaky([exc])
    with pytest.raises(type(exc)):
        with_retry(fn, max_retries=3)
    assert fn.calls["n"] == 1


@pytest.mark.parametrize("exc", [socket.timeout(), ConnectionResetError(), ValueError("x")])
def test_transport_exceptions_are_not_retried_today(exc):
    # Pinned current behaviour. Widening to transport errors is a Phase 4 decision.
    fn = flaky([exc])
    with pytest.raises(type(exc)):
        with_retry(fn, max_retries=3)
    assert fn.calls["n"] == 1


def test_honours_caller_attempt_count():
    fn = flaky([http_error(500)] * 5)
    assert with_retry(fn, max_retries=5) == "ok"
    assert fn.calls["n"] == 6


def test_analytics_wrapper_delegates_and_defaults_to_three(monkeypatch):
    seen = {}

    def fake_with_retry(fn, max_retries=None):
        seen["max_retries"] = max_retries
        return fn()

    monkeypatch.setattr("youtube_analytics_api.with_retry", fake_with_retry)
    assert ya.YouTubeAnalyticsAPI._api_call_with_retry(lambda: "ok") == "ok"
    assert seen["max_retries"] == 3


def test_backfill_wrapper_delegates_and_defaults_to_five(monkeypatch):
    assert backfill_analytics.BACKFILL_MAX_RETRIES == 5
    seen = {}

    def fake_with_retry(fn, max_retries=None):
        seen["max_retries"] = max_retries
        return fn()

    monkeypatch.setattr("backfill_analytics.with_retry", fake_with_retry)
    assert backfill_analytics.api_call_with_retry(lambda: "ok") == "ok"
    assert seen["max_retries"] == 5


def test_backfill_wrapper_really_makes_six_attempts():
    fn = flaky([http_error(500)] * 5)
    assert backfill_analytics.api_call_with_retry(fn) == "ok"
    assert fn.calls["n"] == 6
