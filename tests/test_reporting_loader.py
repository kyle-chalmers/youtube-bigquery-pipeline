"""Loader decisions and the transactional replace script, offline."""

from datetime import datetime, timezone

import pytest

import partition_replacer as pr
from report_specs import SPECS
from reporting_loader import IngestLedger, ReportingLoader, RunSummary
from youtube_reporting_api import ReportRef

REACH = SPECS["channel_reach_basic_a1"]
HDR = "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"


def ref(rid, day, created, job="job-reach", rtype="channel_reach_basic_a1"):
    t = datetime.fromisoformat(f"{day}T07:00:00+00:00")
    return ReportRef(rid, job, rtype, t, t, datetime.fromisoformat(created).replace(tzinfo=timezone.utc), "https://x/" + rid)


class FakeClient:
    def __init__(self, jobs, reports, bodies):
        self._jobs = jobs
        self._reports = reports
        self._bodies = bodies
        self.downloaded = []

    def list_jobs(self):
        return self._jobs

    def list_reports(self, job_id, rtype):
        return sorted(self._reports.get(job_id, []), key=lambda r: (r.start_time, r.create_time))

    def download(self, r):
        self.downloaded.append(r.report_id)
        body = self._bodies[r.report_id]
        if isinstance(body, Exception):
            raise body
        return body


class FakeLedger(IngestLedger):
    def __init__(self, state=None, partition_counts=None):
        self.state = state or {}
        self.marks = []
        self.partition_counts = partition_counts or {}

    def load_state(self):
        return dict(self.state)

    def mark(self, r, status, **kw):
        self.marks.append((r.report_id, status, kw.get("error")))

    def partition_row_count(self, dataset_ref, spec, report_date):
        return self.partition_counts.get(report_date, 0)


class FakeReplacer:
    def __init__(self, refuse=False):
        self.calls = []
        self.refuse = refuse

    def replace_partition(self, spec, rows, provenance):
        if self.refuse:
            raise pr.ReplaceRefused("refused: test")
        self.calls.append((spec.report_type, rows[0]["report_date"], len(rows), provenance["report_id"]))
        return len(rows)


def loaded_row(rid, job, day, created):
    return {"report_id": rid, "job_id": job, "report_date": day, "status": "loaded",
            "report_create_time": datetime.fromisoformat(created).replace(tzinfo=timezone.utc)}


JOBS = [{"id": "job-reach", "reportTypeId": "channel_reach_basic_a1", "name": "kc_reach"}]


def make(reports, bodies, ledger=None, replacer=None, **kw):
    client = FakeClient(JOBS, {"job-reach": reports}, bodies)
    ledger = ledger or FakeLedger()
    replacer = replacer or FakeReplacer()
    loader = ReportingLoader(client, replacer, ledger, "p.ds", archive=None, **kw)
    return loader, client, ledger, replacer


def test_newest_generation_loads_and_older_is_skipped_without_download():
    old = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    new = ref("new", "2026-08-31", "2026-09-05T07:39:00")
    loader, client, ledger, replacer = make([old, new], {"new": (HDR + "20260831,UC1,v1,5,0.2\n").encode()})
    s = loader.run()
    assert client.downloaded == ["new"]
    assert replacer.calls == [("channel_reach_basic_a1", "2026-08-31", 1, "new")]
    assert ("old", "skipped_older", None) in ledger.marks
    assert s.loaded == 1 and s.skipped_older == 1 and s.rows == 1 and s.failed == 0


def test_already_loaded_current_generation_is_not_reloaded():
    new = ref("new", "2026-08-31", "2026-09-05T07:39:00")
    ledger = FakeLedger(state={"new": loaded_row("new", "job-reach", "2026-08-31", "2026-09-05T07:39:00")})
    loader, client, _, replacer = make([new], {}, ledger=ledger)
    s = loader.run()
    assert client.downloaded == [] and replacer.calls == []
    assert s.skipped_current == 1


def test_stale_generation_is_skipped_when_a_newer_one_is_loaded():
    # The API lists only 'old' (newer aged out or filtered), but the ledger already has a newer load.
    old = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    ledger = FakeLedger(state={"new": loaded_row("new", "job-reach", "2026-08-31", "2026-09-05T07:39:00")})
    loader, client, _, replacer = make([old], {}, ledger=ledger)
    s = loader.run()
    assert client.downloaded == [] and s.skipped_current == 1


def test_regeneration_over_a_loaded_day_counts_as_superseded():
    newer = ref("newer", "2026-08-31", "2026-09-06T01:00:00")
    ledger = FakeLedger(state={"new": loaded_row("new", "job-reach", "2026-08-31", "2026-09-05T07:39:00")})
    loader, _, _, replacer = make([newer], {"newer": (HDR + "20260831,UC1,v1,6,0.2\n").encode()}, ledger=ledger)
    s = loader.run()
    assert replacer.calls[0][3] == "newer" and s.superseded == 1 and s.loaded == 1


def test_failed_report_is_ledgered_and_retried_next_run():
    bad = ref("bad", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, replacer = make([bad], {"bad": RuntimeError("503 boom")})
    s = loader.run()
    assert s.failed == 1 and replacer.calls == []
    assert ledger.marks[0][0:2] == ("bad", "failed") and "503 boom" in ledger.marks[0][2]
    # next run: the ledger says failed, not loaded, so it is a candidate again
    ledger2 = FakeLedger(state={"bad": {"report_id": "bad", "job_id": "job-reach", "report_date": "2026-08-31",
                                        "status": "failed", "report_create_time": bad.create_time}})
    loader2, client2, _, _ = make([bad], {"bad": (HDR + "20260831,UC1,v1,1,0\n").encode()}, ledger=ledger2)
    s2 = loader2.run()
    assert client2.downloaded == ["bad"] and s2.loaded == 1


def test_header_only_report_is_ledgered_and_never_replaces():
    ho = ref("ho", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, replacer = make([ho], {"ho": HDR.encode()})
    s = loader.run()
    assert replacer.calls == [] and s.header_only == 1
    assert ledger.marks[0][:2] == ("ho", "header_only")


def test_header_only_over_populated_day_is_a_conflict(caplog):
    ho = ref("ho", "2026-08-31", "2026-09-06T07:39:00")
    ledger = FakeLedger(state={"new": loaded_row("new", "job-reach", "2026-08-31", "2026-09-05T07:39:00")},
                        partition_counts={"2026-08-31": 121})
    loader, _, ledger, replacer = make([ho], {"ho": HDR.encode()}, ledger=ledger)
    with caplog.at_level("WARNING"):
        s = loader.run()
    assert replacer.calls == [] and s.header_only_conflict == 1
    assert ledger.marks[0][:2] == ("ho", "header_only_conflict")
    assert any("Reporting header-only report supersedes populated day" in r.getMessage() for r in caplog.records)


def test_schema_drift_is_a_failure_not_a_load():
    drift = ref("d", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, replacer = make([drift], {"d": b"date,channel_id,video_id,surprise\n20260831,UC1,v1,1\n"})
    s = loader.run()
    assert s.failed == 1 and replacer.calls == []
    assert "SchemaDriftError" in ledger.marks[0][2]


def test_refused_replace_is_recorded_as_failed():
    r = ref("r", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, _ = make([r], {"r": (HDR + "20260831,UC1,v1,1,0\n").encode()}, replacer=FakeReplacer(refuse=True))
    s = loader.run()
    assert s.failed == 1 and ledger.marks[0][:2] == ("r", "failed")


def test_max_reports_per_run_takes_oldest_first_and_reports_deferred():
    refs = [ref(f"r{i}", f"2026-08-{10 + i:02d}", "2026-09-05T00:00:00") for i in range(5)]
    bodies = {r.report_id: (HDR + f"2026{r.report_date[5:7]}{r.report_date[8:]},UC1,v1,1,0\n").encode() for r in refs}
    loader, client, _, _ = make(refs, bodies, max_reports_per_run=2)
    s = loader.run()
    assert client.downloaded == ["r0", "r1"] and s.deferred == 3


def test_unregistered_report_type_is_skipped_with_warning(caplog):
    client = FakeClient([{"id": "j", "reportTypeId": "channel_mystery_a9", "name": "x"}], {}, {})
    loader = ReportingLoader(client, FakeReplacer(), FakeLedger(), "p.ds", archive=None)
    with caplog.at_level("WARNING"):
        s = loader.run()
    assert s.reports_considered == 0
    assert any("unregistered report type" in r.getMessage() for r in caplog.records)


# --- the transaction script ---------------------------------------------------------

def test_replace_script_asserts_before_it_deletes():
    script = pr.build_replace_script("p.ds", REACH, "_load_x")
    delete_at = script.index("DELETE FROM `p.ds.reporting_channel_reach_basic_a1`")
    for guard in [
        "work table has zero rows",
        "not all for the expected report_date",
        "channel other than the configured one",
        "native grain is not unique",
        "this report_id is already loaded",
        "equal or newer generation of this day is already loaded",
    ]:
        assert script.index(guard) < delete_at, guard
    assert "BEGIN TRANSACTION" in script and "COMMIT TRANSACTION" in script and "ROLLBACK TRANSACTION" in script
    assert "GROUP BY report_date, channel_id, video_id HAVING COUNT(*) > 1" in script
    assert "report_create_time >= @report_create_time" in script


def test_replace_script_uses_explicit_column_lists():
    script = pr.build_replace_script("p.ds", REACH, "_load_x")
    cols = "report_date, channel_id, video_id, video_thumbnail_impressions, video_thumbnail_impressions_ctr, report_id, report_create_time, job_id, load_source, ingested_at"
    assert f"INSERT INTO `p.ds.reporting_channel_reach_basic_a1` ({cols}) SELECT {cols} FROM `p.ds._load_x`" in script


def test_replacer_refuses_zero_rows_before_any_job():
    class NoClient:
        def __getattr__(self, name):
            raise AssertionError("BigQuery must not be touched for zero rows")

    r = pr.StagedTransactionalReplacer(NoClient(), "p.ds", "UC1")
    with pytest.raises(pr.ReplaceRefused):
        r.replace_partition(REACH, [], {"report_id": "x"})


def test_work_table_schema_requires_only_date_and_channel():
    fields = {f.name: f.mode for f in pr._schema_for(SPECS["channel_basic_a3"])}
    assert fields["report_date"] == "REQUIRED" and fields["channel_id"] == "REQUIRED"
    assert fields["video_id"] == "NULLABLE" and fields["country_code"] == "NULLABLE"
    assert fields["views"] == "NULLABLE" and fields["report_id"] == "REQUIRED"


def test_run_summary_as_dict_round_trips():
    s = RunSummary(loaded=2, rows=10)
    d = s.as_dict()
    assert d["loaded"] == 2 and d["rows"] == 10 and d["errors"] == []


# --- review-driven additions (Phase 2 gate) --------------------------------------

def test_generic_replacer_error_is_ledgered_failed_and_the_run_continues():
    class BoomThenOk:
        def __init__(self):
            self.calls = 0

        def replace_partition(self, spec, rows, provenance):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("503 backendError")
            return len(rows)

    r1 = ref("r1", "2026-08-30", "2026-09-01T00:00:00")
    r2 = ref("r2", "2026-08-31", "2026-09-02T00:00:00")
    bodies = {"r1": (HDR + "20260830,UC1,v1,1,0\n").encode(), "r2": (HDR + "20260831,UC1,v1,1,0\n").encode()}
    loader, client, ledger, replacer = make([r1, r2], bodies, replacer=BoomThenOk())
    s = loader.run()
    assert s.failed == 1 and s.loaded == 1
    assert client.downloaded == ["r1", "r2"], "the second report must still be attempted"
    assert ledger.marks[0][:2] == ("r1", "failed") and "backendError" in ledger.marks[0][2]


def test_already_loaded_refusal_is_a_skip_with_no_ledger_write():
    class Racer:
        def replace_partition(self, spec, rows, provenance):
            raise pr.AlreadyLoaded("already_loaded: this report_id is already loaded")

    r = ref("r", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, _ = make([r], {"r": (HDR + "20260831,UC1,v1,1,0\n").encode()}, replacer=Racer())
    s = loader.run()
    assert s.skipped_current == 1 and s.failed == 0
    assert ledger.marks == [], "the winning run's loaded row must not be touched"


def test_failed_older_generation_is_re_ledgered_as_skipped_older():
    old = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    new = ref("new", "2026-08-31", "2026-09-05T07:39:00")
    ledger = FakeLedger(state={"old": {"report_id": "old", "job_id": "job-reach", "report_date": "2026-08-31",
                                       "status": "failed", "report_create_time": old.create_time}})
    loader, client, ledger, _ = make([old, new], {"new": (HDR + "20260831,UC1,v1,1,0\n").encode()}, ledger=ledger)
    s = loader.run()
    assert ("old", "skipped_older", None) in ledger.marks
    assert client.downloaded == ["new"] and s.loaded == 1


def test_loaded_older_generation_is_never_re_marked():
    old = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    new = ref("new", "2026-08-31", "2026-09-05T07:39:00")
    ledger = FakeLedger(state={"old": loaded_row("old", "job-reach", "2026-08-31", "2026-09-02T22:11:00")})
    loader, _, ledger, _ = make([old, new], {"new": (HDR + "20260831,UC1,v1,1,0\n").encode()}, ledger=ledger)
    loader.run()
    assert not any(m[0] == "old" for m in ledger.marks)


def test_retries_of_failed_reports_go_after_fresh_ones_and_are_capped():
    failed_refs = [ref(f"f{i}", f"2026-08-{10 + i:02d}", "2026-09-01T00:00:00") for i in range(7)]
    fresh_ref = ref("fresh", "2026-08-30", "2026-09-02T00:00:00")
    state = {r.report_id: {"report_id": r.report_id, "job_id": "job-reach", "report_date": r.report_date,
                           "status": "failed", "report_create_time": r.create_time} for r in failed_refs}
    bodies = {r.report_id: (HDR + f"2026{r.report_date[5:7]}{r.report_date[8:]},UC1,v1,1,0\n").encode()
              for r in failed_refs + [fresh_ref]}
    loader, client, _, _ = make(failed_refs + [fresh_ref], bodies, ledger=FakeLedger(state=state),
                                max_reports_per_run=30)
    s = loader.run()
    assert client.downloaded[0] == "fresh", "never-attempted reports go first"
    assert len(client.downloaded) == 1 + 5 and s.retried_failed == 5 and s.deferred == 2


def test_header_only_newest_with_unloaded_populated_older_sibling_is_a_conflict(caplog):
    old = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    ho = ref("ho", "2026-08-31", "2026-09-05T07:39:00")
    bodies = {"ho": HDR.encode(), "old": (HDR + "20260831,UC1,v1,9,0.1\n").encode()}
    loader, client, ledger, replacer = make([old, ho], bodies)
    with caplog.at_level("WARNING"):
        s = loader.run()
    assert s.header_only_conflict == 1 and replacer.calls == []
    assert "old" in client.downloaded, "the older generation is inspected before the empty one is trusted"
    assert any("never loaded" in (m[2] or "") for m in ledger.marks if m[1] == "header_only_conflict")


def test_header_only_newest_with_header_only_older_sibling_is_fine():
    old = ref("old", "2026-08-31", "2026-09-02T22:11:00")
    ho = ref("ho", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, _ = make([old, ho], {"ho": HDR.encode(), "old": HDR.encode()})
    s = loader.run()
    assert s.header_only == 1 and s.header_only_conflict == 0


def test_csv_date_disagreeing_with_report_day_is_a_failure():
    r = ref("r", "2026-08-31", "2026-09-05T07:39:00")
    loader, _, ledger, replacer = make([r], {"r": (HDR + "20260830,UC1,v1,1,0\n").encode()})
    s = loader.run()
    assert s.failed == 1 and replacer.calls == [] and "disagrees" in ledger.marks[0][2]


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name, self.metadata, self.content_encoding = store, name, None, None

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        from google.api_core.exceptions import PreconditionFailed
        assert if_generation_match == 0
        if self.name in self.store:
            raise PreconditionFailed("exists")
        self.store[self.name] = dict(self.metadata)

    def reload(self):
        self.metadata = self.store[self.name]


class FakeBucket:
    name = "bkt"

    def __init__(self):
        self.store = {}

    def blob(self, name):
        return FakeBlob(self.store, name)


def test_archive_stores_before_parse_and_gcs_uri_reaches_the_ledger():
    from reporting_loader import GcsArchive
    order = []

    class OrderClient(FakeClient):
        def download(self, r):
            order.append("download")
            return super().download(r)

    bucket = FakeBucket()
    r = ref("r", "2026-08-31", "2026-09-05T07:39:00")
    client = OrderClient(JOBS, {"job-reach": [r]}, {"r": b"date,channel_id,video_id,surprise\n20260831,UC1,v1,1\n"})
    ledger = FakeLedger()
    loader = ReportingLoader(client, FakeReplacer(), ledger, "p.ds", GcsArchive(bucket))
    s = loader.run()
    # schema drift -> failed, but the body was archived first (continuous archive)
    assert s.failed == 1
    assert "channel_reach_basic_a1/2026-08-31/r.csv.gz" in bucket.store
    meta = bucket.store["channel_reach_basic_a1/2026-08-31/r.csv.gz"]
    assert set(meta) == {"job_id", "report_id", "report_type", "report_date", "start_time", "end_time",
                         "create_time", "csv_sha256", "csv_bytes", "data_rows"}
    assert meta["data_rows"] == "1"


def test_archive_precondition_failed_compares_hashes():
    from reporting_loader import GcsArchive
    bucket = FakeBucket()
    r = ref("r", "2026-08-31", "2026-09-05T07:39:00")
    a = GcsArchive(bucket)
    uri = a.store(r, b"a,b\n1,2\n", "sha-1", 1)
    assert uri == "gs://bkt/channel_reach_basic_a1/2026-08-31/r.csv.gz"
    assert a.store(r, b"a,b\n1,2\n", "sha-1", 1) == uri  # same bytes: fine
    with pytest.raises(RuntimeError):
        a.store(r, b"a,b\n9,9\n", "sha-2", 1)  # different bytes under the same report_id: refused


def test_ledger_mark_never_overwrites_a_loaded_row_in_sql():
    captured = {}

    class C:
        def query(self, sql, job_config=None):
            captured["sql"] = sql
            captured["params"] = {p.name: (p.type_, p.value) for p in job_config.query_parameters}

            class R:
                def result(self):
                    return None
            return R()

    from reporting_loader import IngestLedger
    IngestLedger(C(), "p.ds").mark(ref("r", "2026-08-31", "2026-09-05T07:39:00"), "failed", error="x" * 2000)
    assert "WHEN MATCHED AND L.status != 'loaded' THEN UPDATE" in captured["sql"]
    import re
    placeholders = set(re.findall(r"@(\w+)", captured["sql"]))
    assert placeholders == set(captured["params"]), "every @placeholder must be bound"
    assert len(captured["params"]["error"][1]) == 1000
    assert captured["params"]["row_count"] == ("INT64", None)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr("partition_replacer.time.sleep", lambda s: None)


def test_classify_error_and_retry_with_backoff_on_transient():
    assert pr.classify_error("already_loaded: this report_id is already loaded") == "already_loaded"
    assert pr.classify_error("refused: native grain is not unique") == "refused"
    assert pr.classify_error("Transaction is aborted due to concurrent update") == "transient"
    assert pr.classify_error("An internal error occurred (internalError)") == "transient"
    assert pr.classify_error("Not found: Table x") == "other"

    class Client:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0

        def query(self, sql, job_config=None):
            self.calls += 1
            if self.errors:
                raise RuntimeError(self.errors.pop(0))

            class R:
                def result(self):
                    return None
            return R()

    rep = pr.StagedTransactionalReplacer(Client(["concurrent update"]), "p.ds", "UC1")
    rep._run_with_retries("x", [])
    assert rep.client.calls == 2
    # the loser of a concurrent run: aborted twice while the winner runs, then the winner's
    # committed ledger row makes the third attempt refuse with already_loaded
    rep = pr.StagedTransactionalReplacer(Client(["Transaction is aborted due to concurrent update"] * 2 + ["already_loaded: an equal or newer generation"]), "p.ds", "UC1")
    with pytest.raises(pr.AlreadyLoaded):
        rep._run_with_retries("x", [])
    assert rep.client.calls == 3
    rep = pr.StagedTransactionalReplacer(Client(["Transaction is aborted due to concurrent update"] * 4), "p.ds", "UC1")
    with pytest.raises(RuntimeError):
        rep._run_with_retries("x", [])
    assert rep.client.calls == 4, "initial + 3 retries, then give up"
    rep = pr.StagedTransactionalReplacer(Client(["Not found"]), "p.ds", "UC1")
    with pytest.raises(RuntimeError):
        rep._run_with_retries("x", [])
    assert rep.client.calls == 1
    rep = pr.StagedTransactionalReplacer(Client(["already_loaded: x"]), "p.ds", "UC1")
    with pytest.raises(pr.AlreadyLoaded):
        rep._run_with_retries("x", [])


def test_work_table_name_is_unique_per_call_and_expiry_set_at_creation():
    rep = pr.StagedTransactionalReplacer(object(), "p.ds", "UC1")
    a = rep._work_table_name(REACH, "17598211089")
    b = rep._work_table_name(REACH, "17598211089")
    assert a != b and a.startswith("_load_channel_reach_basic_a1_17598211089_")

    class Client:
        def __init__(self):
            self.events = []

        def create_table(self, table):
            self.events.append(("create", table.expires is not None))

        def load_table_from_file(self, fh, table_ref, job_config=None):
            self.events.append(("load", job_config.write_disposition))

            class J:
                def result(self):
                    return None
            return J()

    c = Client()
    pr.StagedTransactionalReplacer(c, "p.ds", "UC1")._stage(REACH, "_load_x", [{"report_date": "2026-08-31"}])
    assert c.events == [("create", True), ("load", "WRITE_TRUNCATE")]


def test_replace_partition_does_not_mutate_caller_rows():
    class C:
        def create_table(self, t): pass

        def load_table_from_file(self, fh, ref, job_config=None):
            class J:
                def result(self): return None
            return J()

        def query(self, sql, job_config=None):
            class J:
                def result(self): return None
            return J()

        def delete_table(self, ref, not_found_ok=True): pass

    rows = [{"report_date": "2026-08-31", "channel_id": "UC1", "video_id": "v", "video_thumbnail_impressions": 1,
             "video_thumbnail_impressions_ctr": 0.0}]
    before = [dict(r) for r in rows]
    pr.StagedTransactionalReplacer(C(), "p.ds", "UC1").replace_partition(
        REACH, rows, {"report_id": "r", "job_id": "j", "report_create_time": "2026-09-05T00:00:00+00:00", "load_source": "t"})
    assert rows == before
