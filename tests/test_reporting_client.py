"""Offline tests for the Reporting API client: listing, pagination, newest-wins, download."""

import gzip
from datetime import datetime, timezone

import pytest
import requests

from youtube_reporting_api import (
    ReportDownloadError,
    ReportRef,
    YouTubeReportingClient,
    newest_per_day,
)


def ref(report_id, day, created, report_type="channel_reach_basic_a1", job_id="job-1"):
    start = datetime.fromisoformat(f"{day}T07:00:00+00:00")
    return ReportRef(
        report_id=report_id,
        job_id=job_id,
        report_type=report_type,
        start_time=start,
        end_time=datetime.fromisoformat(f"{day}T07:00:00+00:00"),
        create_time=datetime.fromisoformat(created).replace(tzinfo=timezone.utc),
        download_url=f"https://example.invalid/{report_id}",
    )


def test_report_date_is_the_utc_date_of_start_time():
    # midnight Pacific during daylight saving is 07:00Z; during standard time 08:00Z
    assert ref("r", "2026-08-31", "2026-09-02T10:00:00").report_date == "2026-08-31"
    winter = ReportRef(
        report_id="w", job_id="j", report_type="t",
        start_time=datetime.fromisoformat("2026-01-15T08:00:00+00:00"),
        end_time=datetime.fromisoformat("2026-01-16T08:00:00+00:00"),
        create_time=datetime.fromisoformat("2026-01-17T08:00:00+00:00"),
        download_url="u",
    )
    assert winter.report_date == "2026-01-15"


def test_newest_per_day_keeps_only_newest_create_time_regardless_of_order():
    older = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    newer = ref("new", "2026-08-31", "2026-09-05T07:39:00")
    other = ref("other", "2026-09-01", "2026-09-03T05:48:00")
    for order in ([older, newer, other], [newer, older, other], [other, newer, older]):
        picked = newest_per_day(order)
        assert picked["2026-08-31"].report_id == "new"
        assert picked["2026-09-01"].report_id == "other"
        assert len(picked) == 2


# --- fakes for the discovery client -------------------------------------------------

class FakeListable:
    """Mimics resource.list(**kwargs).execute() over a dict of pages keyed by pageToken."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        page = self.pages[kwargs.get("pageToken", "first")]

        class _Req:
            def execute(_self):
                return page

        return _Req()


class FakeService:
    def __init__(self, reports=None, jobs=None, types=None):
        self._reports = reports or FakeListable({"first": {}})
        self._jobs = jobs or FakeListable({"first": {}})
        self._types = types or FakeListable({"first": {}})

    def jobs(self):
        outer = self

        class _Jobs:
            def reports(_self):
                return outer._reports

            def list(_self, **kw):
                return outer._jobs.list(**kw)

        return _Jobs()

    def reportTypes(self):  # noqa: N802 - mirrors the discovery client
        return self._types


def make_client(monkeypatch, service):
    monkeypatch.setattr("youtube_reporting_api.build", lambda *a, **k: service)
    return YouTubeReportingClient(credentials=object())


def api_report(rid, day, created):
    return {
        "id": rid,
        "jobId": "job-1",
        "startTime": f"{day}T07:00:00Z",
        "endTime": f"{day}T07:00:00Z",
        "createTime": created,
        "downloadUrl": f"https://example.invalid/{rid}",
    }


def test_list_reports_follows_pagination_and_sorts(monkeypatch):
    pages = {
        "first": {
            "reports": [api_report("b", "2026-09-02", "2026-09-05T05:10:00.123456Z")],
            "nextPageToken": "p2",
        },
        "p2": {"reports": [api_report("a", "2026-09-01", "2026-09-03T05:48:00Z")]},
    }
    res = FakeListable(pages)
    client = make_client(monkeypatch, FakeService(reports=res))
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    refs = client.list_reports("job-1", "channel_reach_basic_a1", created_after=since)
    assert [r.report_id for r in refs] == ["a", "b"]  # sorted by start_time
    assert res.calls[0]["createdAfter"] == "2026-09-01T00:00:00.000000Z"
    assert res.calls[0]["jobId"] == "job-1"
    assert "createdAfter" not in {k for k in res.calls[0]} - {"createdAfter"}  # present on both pages
    assert res.calls[1]["pageToken"] == "p2"
    assert all(c["pageSize"] == 200 for c in res.calls)
    assert refs[1].create_time.microsecond == 123456


def test_list_reports_without_created_after_omits_the_parameter(monkeypatch):
    res = FakeListable({"first": {"reports": []}})
    client = make_client(monkeypatch, FakeService(reports=res))
    assert client.list_reports("job-1", "t") == []
    assert "createdAfter" not in res.calls[0]


def test_list_reports_rejects_naive_created_after(monkeypatch):
    client = make_client(monkeypatch, FakeService())
    with pytest.raises(ValueError):
        client.list_reports("job-1", "t", created_after=datetime(2026, 9, 1))


def test_list_jobs_and_report_types_paginate_and_drop_system_managed(monkeypatch):
    jobs = FakeListable({
        "first": {"jobs": [{"id": "j1", "reportTypeId": "a"}], "nextPageToken": "n"},
        "n": {"jobs": [{"id": "j2", "reportTypeId": "b"}, {"id": "sys", "reportTypeId": "c", "systemManaged": True}]},
    })
    types = FakeListable({
        "first": {"reportTypes": [{"id": "a"}], "nextPageToken": "n"},
        "n": {"reportTypes": [{"id": "b"}, {"id": "sys", "systemManaged": True}]},
    })
    client = make_client(monkeypatch, FakeService(jobs=jobs, types=types))
    assert [j["id"] for j in client.list_jobs()] == ["j1", "j2"]
    assert [t["id"] for t in client.list_report_types()] == ["a", "b"]
    assert jobs.calls[1]["pageToken"] == "n"
    assert types.calls[1]["pageToken"] == "n"


def test_pagination_refuses_a_repeated_token(monkeypatch):
    jobs = FakeListable({"first": {"jobs": [], "nextPageToken": "loop"}, "loop": {"jobs": [], "nextPageToken": "loop"}})
    client = make_client(monkeypatch, FakeService(jobs=jobs))
    with pytest.raises(RuntimeError):
        client.list_jobs()


# --- download ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, content, status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.responses.pop(0)


CSV = b"date,channel_id,video_id\n20260903,UC1,v1\n"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("retry.time.sleep", lambda s: None)


def test_download_inflates_gzip_and_asks_for_gzip(monkeypatch):
    client = make_client(monkeypatch, FakeService())
    client._session = FakeSession([FakeResponse(gzip.compress(CSV))])
    r = ref("r", "2026-09-03", "2026-09-05T12:20:00")
    assert client.download(r) == CSV
    assert client._session.calls[0][1] == {"Accept-Encoding": "gzip"}

    client._session = FakeSession([FakeResponse(CSV)])  # already inflated by the transport
    assert client.download(r) == CSV


def test_download_retries_transient_status_then_succeeds(monkeypatch):
    client = make_client(monkeypatch, FakeService())
    client._session = FakeSession([FakeResponse(b"", status=503), FakeResponse(CSV)])
    assert client.download(ref("r", "2026-09-03", "2026-09-05T12:20:00")) == CSV
    assert len(client._session.calls) == 2


def test_download_does_not_retry_403_and_never_leaks_the_url(monkeypatch):
    client = make_client(monkeypatch, FakeService())
    client._session = FakeSession([FakeResponse(b"", status=403)])
    with pytest.raises(ReportDownloadError) as exc:
        client.download(ref("r", "2026-09-03", "2026-09-05T12:20:00"))
    assert len(client._session.calls) == 1
    assert "HTTP 403" in str(exc.value) and "example.invalid" not in str(exc.value)


def test_download_exhausted_retries_surface_as_sanitized_error(monkeypatch):
    client = make_client(monkeypatch, FakeService())
    client._session = FakeSession([FakeResponse(b"", status=503)] * 4)
    with pytest.raises(ReportDownloadError) as exc:
        client.download(ref("r", "2026-09-03", "2026-09-05T12:20:00"))
    assert len(client._session.calls) == 4 and "HTTP 503" in str(exc.value)


@pytest.mark.parametrize(
    "body, headers",
    [
        (b"", {}),                                            # empty body
        (b"not a csv header\nrow\n", {}),                     # no comma in first line
        (CSV[:10], {"Content-Length": str(len(CSV))}),        # truncated vs Content-Length
    ],
)
def test_download_rejects_bad_bodies(monkeypatch, body, headers):
    client = make_client(monkeypatch, FakeService())
    client._session = FakeSession([FakeResponse(body, headers=headers)])
    with pytest.raises(ReportDownloadError):
        client.download(ref("r", "2026-09-03", "2026-09-05T12:20:00"))


def test_download_enforces_size_cap(monkeypatch):
    client = make_client(monkeypatch, FakeService())
    client._session = FakeSession([FakeResponse(CSV)])
    with pytest.raises(ReportDownloadError):
        client.download(ref("r", "2026-09-03", "2026-09-05T12:20:00"), max_bytes=10)
