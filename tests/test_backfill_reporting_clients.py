"""The replay clients in setup/backfill_reporting.py: archive listing and single-report restriction."""

import gzip
from datetime import datetime, timezone

import backfill_reporting as br
from youtube_reporting_api import ReportRef


class FakeBlob:
    def __init__(self, name, metadata, body=b""):
        self.name = name
        self.metadata = metadata
        self._body = body
        self.raw_download_seen = None

    def download_as_bytes(self, raw_download=False):
        self.raw_download_seen = raw_download
        # A real GCS object with content_encoding=gzip is inflated by the client unless raw_download=True.
        return gzip.compress(self._body) if raw_download else self._body


class FakeBucket:
    name = "bkt"

    def __init__(self, blobs):
        self._blobs = blobs

    def list_blobs(self, prefix=""):
        return [b for b in self._blobs if b.name.startswith(prefix)]

    def blob(self, name):
        return next(b for b in self._blobs if b.name == name)


def meta(rid, job, day, created, rtype="channel_reach_basic_a1"):
    t = f"{day}T07:00:00+00:00"
    return {"report_id": rid, "job_id": job, "report_type": rtype,
            "start_time": t, "end_time": t, "create_time": created}


BLOBS = [
    FakeBlob("channel_reach_basic_a1/2026-08-31/old.csv.gz", meta("old", "job-reach", "2026-08-31", "2026-09-02T22:11:00+00:00")),
    FakeBlob("channel_reach_basic_a1/2026-08-31/new.csv.gz", meta("new", "job-reach", "2026-08-31", "2026-09-05T07:39:00+00:00")),
    FakeBlob("channel_reach_basic_a1/2026-09-01/x.csv.gz", meta("x", "other-job", "2026-09-01", "2026-09-03T00:00:00+00:00")),
    FakeBlob("channel_basic_a3/2026-08-31/b.csv.gz", meta("b", "job-basic", "2026-08-31", "2026-09-02T00:00:00+00:00", "channel_basic_a3")),
    FakeBlob("stray/no-metadata.txt", {}),
]


def test_archive_client_builds_jobs_and_reports_from_metadata_without_youtube():
    client = br.ArchiveClient(FakeBucket(BLOBS))
    assert client.list_jobs() == [
        {"id": "job-basic", "reportTypeId": "channel_basic_a3", "name": "archive:channel_basic_a3"},
        {"id": "job-reach", "reportTypeId": "channel_reach_basic_a1", "name": "archive:channel_reach_basic_a1"},
        {"id": "other-job", "reportTypeId": "channel_reach_basic_a1", "name": "archive:channel_reach_basic_a1"},
    ]
    refs = client.list_reports("job-reach", "channel_reach_basic_a1")
    assert [r.report_id for r in refs] == ["old", "new"], "sorted by (start, create); other job excluded"
    assert refs[0].report_date == "2026-08-31"
    assert refs[0].create_time == datetime(2026, 9, 2, 22, 11, tzinfo=timezone.utc)
    assert refs[0].download_url == "gs://bkt/channel_reach_basic_a1/2026-08-31/old.csv.gz"


def test_archive_client_filters_and_downloads_raw_then_inflates():
    body = b"date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
    blob = FakeBlob("channel_reach_basic_a1/2026-08-31/old.csv.gz", meta("old", "job-reach", "2026-08-31", "2026-09-02T22:11:00+00:00"), body)
    client = br.ArchiveClient(FakeBucket([blob] + BLOBS[1:]), only_type="channel_reach_basic_a1")
    assert {j["id"] for j in client.list_jobs()} == {"job-reach", "other-job"}
    ref = client.list_reports("job-reach", "channel_reach_basic_a1")[0]
    assert client.download(ref) == body
    assert blob.raw_download_seen is True, "must fetch the stored gzip bytes, not the transport-inflated body"
    by_job = br.ArchiveClient(FakeBucket(BLOBS), only_job="job-basic")
    assert [j["id"] for j in by_job.list_jobs()] == ["job-basic"]
    since = br.ArchiveClient(FakeBucket(BLOBS), since="2026-09-01")
    assert [r.report_id for r in since.list_reports("job-reach", "channel_reach_basic_a1")] == []
    assert [r.report_id for r in since.list_reports("other-job", "channel_reach_basic_a1")] == ["x"]


def test_empty_replace_script_validates_before_deleting():
    script = br.empty_replace_script("p.ds", "reporting_channel_reach_basic_a1")
    assert script.index("status = 'header_only_conflict'") < script.index("DELETE FROM `p.ds.reporting_channel_reach_basic_a1`")
    assert "BEGIN TRANSACTION" in script and "ROLLBACK TRANSACTION" in script and "rows_removed" in script


def test_only_report_restricts_to_one_report_id():
    class Inner:
        def list_jobs(self):
            return [{"id": "j"}]

        def list_reports(self, job_id, rtype):
            t = datetime(2026, 8, 31, 7, tzinfo=timezone.utc)
            return [ReportRef("a", "j", rtype, t, t, t, "u"), ReportRef("b", "j", rtype, t, t, t, "u")]

        def download(self, ref):
            return b"x"

    only = br.OnlyReport(Inner(), "b")
    assert [r.report_id for r in only.list_reports("j", "t")] == ["b"]
    assert only.list_jobs() == [{"id": "j"}]
    assert only.download(None) == b"x"
