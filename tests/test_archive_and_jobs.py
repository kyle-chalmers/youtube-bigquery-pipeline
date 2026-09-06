"""The Phase 0 scripts: archive path contract, idempotency, hardening, and job creation."""

from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import PreconditionFailed
from googleapiclient.errors import HttpError
import httplib2

import archive_reporting_raw as arch
from youtube_reporting_api import ReportRef


def ref(rid="r1", day="2026-08-31", rtype="channel_reach_basic_a1"):
    t = datetime.fromisoformat(f"{day}T07:00:00+00:00")
    return ReportRef(rid, "job-1", rtype, t, t, datetime(2026, 9, 2, tzinfo=timezone.utc), "https://x/" + rid)


def test_object_name_is_type_date_id():
    assert arch.object_name(ref()) == "channel_reach_basic_a1/2026-08-31/r1.csv.gz"


@pytest.mark.parametrize("body, rows", [
    (b"h1,h2\na,b\nc,d\n", 2),
    (b"h1,h2\na,b\nc,d", 2),      # no trailing newline
    (b"h1,h2\n", 0),
    (b"h1,h2", 0),
])
def test_csv_data_rows_ignores_trailing_newline(body, rows):
    assert arch.csv_data_rows(body) == rows


class FakeBlob:
    def __init__(self, name, exists_with_sha=None):
        self.name = name
        self.metadata = None
        self.content_encoding = None
        self.uploads = []
        self._exists_sha = exists_with_sha

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        assert if_generation_match == 0, "must be create-if-absent"
        assert content_type == "text/csv"
        if self._exists_sha is not None:
            raise PreconditionFailed("exists")
        self.uploads.append(data)

    def reload(self):
        pass  # metadata stays as set locally, mirroring a successful round trip


class FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, name):
        assert name == self._blob.name
        return self._blob


class FakeYT:
    def __init__(self, body):
        self.body = body

    def download(self, r):
        return self.body


def test_archive_one_uploads_gzip_with_metadata_and_verifies_sha():
    body = b"date,channel_id\n20260831,UC1\n"
    blob = FakeBlob("channel_reach_basic_a1/2026-08-31/r1.csv.gz")
    status, n, sha = arch.archive_one(FakeBucket(blob), FakeYT(body), ref())
    assert status == "archived" and n == len(body)
    assert blob.metadata["csv_sha256"] == sha
    assert blob.metadata["data_rows"] == "1"
    assert blob.metadata["report_date"] == "2026-08-31"
    assert blob.content_encoding == "gzip"
    import gzip
    assert gzip.decompress(blob.uploads[0]) == body


def test_archive_one_treats_precondition_failed_as_exists():
    blob = FakeBlob("channel_reach_basic_a1/2026-08-31/r1.csv.gz", exists_with_sha="whatever")
    status, _, _ = arch.archive_one(FakeBucket(blob), FakeYT(b"a,b\n1,2\n"), ref())
    assert status == "exists"
    assert blob.uploads == []


def test_archive_one_raises_when_stored_hash_disagrees():
    class LyingBlob(FakeBlob):
        def reload(self):
            self.metadata = dict(self.metadata, csv_sha256="deadbeef")

    blob = LyingBlob("channel_reach_basic_a1/2026-08-31/r1.csv.gz")
    with pytest.raises(RuntimeError):
        arch.archive_one(FakeBucket(blob), FakeYT(b"a,b\n1,2\n"), ref())


class FakeIamConfig:
    def __init__(self, ubla, pap):
        self.uniform_bucket_level_access_enabled = ubla
        self.public_access_prevention = pap


class FakeExistingBucket:
    def __init__(self, ubla, pap):
        self.iam_configuration = FakeIamConfig(ubla, pap)
        self.location = "US-CENTRAL1"
        self.patched = False

    def patch(self):
        self.patched = True

    def reload(self):
        pass


class FakeStorageClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def get_bucket(self, name):
        return self._bucket


def test_ensure_bucket_hardens_an_existing_unhardened_bucket(capsys):
    b = FakeExistingBucket(ubla=False, pap="inherited")
    out = arch.ensure_bucket(FakeStorageClient(b), "bkt", "us-central1", dry_run=False)
    assert out is b and b.patched
    assert b.iam_configuration.uniform_bucket_level_access_enabled is True
    assert b.iam_configuration.public_access_prevention == "enforced"


def test_ensure_bucket_leaves_a_hardened_bucket_alone():
    b = FakeExistingBucket(ubla=True, pap="enforced")
    arch.ensure_bucket(FakeStorageClient(b), "bkt", "us-central1", dry_run=False)
    assert not b.patched


def test_create_jobs_treats_409_as_exists(monkeypatch, capsys):
    import importlib
    cj = importlib.import_module("7_create_reporting_jobs")

    class FakeClient:
        def __init__(self, creds):
            pass

        def list_report_types(self):
            return [{"id": "channel_cards_a1"}, {"id": "channel_basic_a3"}]

        def list_jobs(self):
            return [{"id": "j", "reportTypeId": "channel_basic_a3", "name": "User activity", "createTime": "2025-10-27T00:00:00Z"}]

        def create_job(self, name, report_type_id):
            raise HttpError(httplib2.Response({"status": 409}), b"exists")

    monkeypatch.setattr(cj, "YouTubeReportingClient", FakeClient)
    monkeypatch.setattr(cj, "load_oauth_credentials", lambda p: object())
    monkeypatch.setattr(cj._bootstrap, "resolve_project", lambda: "proj")
    monkeypatch.setattr("sys.argv", ["x", "--create"])
    assert cj.main() == 0
    out = capsys.readouterr().out
    assert "EXISTS  channel_cards_a1" in out
    assert "created=0 already_existed=1 failed=0" in out


def test_create_jobs_fails_when_no_report_types_are_visible(monkeypatch):
    import importlib
    cj = importlib.import_module("7_create_reporting_jobs")

    class FakeClient:
        def __init__(self, creds):
            pass

        def list_report_types(self):
            return []

        def list_jobs(self):
            return []

    monkeypatch.setattr(cj, "YouTubeReportingClient", FakeClient)
    monkeypatch.setattr(cj, "load_oauth_credentials", lambda p: object())
    monkeypatch.setattr(cj._bootstrap, "resolve_project", lambda: "proj")
    monkeypatch.setattr("sys.argv", ["x"])
    assert cj.main() == 1
