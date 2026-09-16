"""Duplicate repair validates the archived source before building any replacement."""

import gzip
import hashlib
import importlib
from datetime import datetime, timezone

import pytest

from report_specs import SPECS


repair = importlib.import_module("repair_reporting_duplicates")
SPEC = SPECS["channel_reach_basic_a1"]
RAW = (
    b"date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
    b"20260907,UC1,v1,100,0.25\n"
    b"20260907,UC1,v2,50,0.5\n"
)
SHA = hashlib.sha256(RAW).hexdigest()
CREATED = datetime(2026, 9, 9, 8, tzinfo=timezone.utc)


def table_rows():
    base = [
        {"report_date": "2026-09-07", "channel_id": "UC1", "video_id": "v1",
         "video_thumbnail_impressions": 100, "video_thumbnail_impressions_ctr": 0.25},
        {"report_date": "2026-09-07", "channel_id": "UC1", "video_id": "v2",
         "video_thumbnail_impressions": 50, "video_thumbnail_impressions_ctr": 0.5},
    ]
    rows = []
    for stamp in ("2026-09-09T08:01:00+00:00", "2026-09-09T08:11:00+00:00"):
        for row in base:
            rows.append(dict(row, report_id="r1", report_create_time=CREATED, job_id="j1",
                             load_source="cron", ingested_at=stamp))
    return rows


def ledger_rows():
    base = {
        "report_id": "r1", "job_id": "j1", "report_type": SPEC.report_type,
        "report_date": "2026-09-07", "report_create_time": CREATED, "status": "loaded",
        "row_count": 2, "csv_bytes": len(RAW), "content_sha256": SHA,
        "gcs_uri": "gs://archive/path.csv.gz", "load_source": "cron", "error": None,
    }
    return [dict(base, ingested_at="2026-09-09T08:01:00+00:00"),
            dict(base, ingested_at="2026-09-09T08:11:00+00:00")]


def test_validation_proves_uniform_duplicates_and_archive_equivalence():
    result = repair.validate_repair_inputs(
        SPEC,
        "2026-09-07",
        table_rows(),
        ledger_rows(),
        RAW,
        {"csv_sha256": SHA, "report_id": "r1", "report_type": SPEC.report_type,
         "report_date": "2026-09-07", "job_id": "j1"},
    )
    assert result.copy_count == 2
    assert result.physical_rows == 4
    assert result.canonical_rows == 2
    assert len(result.replacement_rows) == 2
    assert result.report_id == "r1"
    assert result.sha256 == SHA


def test_validation_rejects_archive_checksum_disagreement():
    with pytest.raises(RuntimeError, match="archive metadata sha256"):
        repair.validate_repair_inputs(
            SPEC, "2026-09-07", table_rows(), ledger_rows(), RAW,
            {"csv_sha256": "deadbeef", "report_id": "r1", "report_type": SPEC.report_type,
             "report_date": "2026-09-07", "job_id": "j1"},
        )


def test_validation_rejects_nonduplicate_or_nonuniform_rows():
    rows = table_rows()
    rows.pop()
    with pytest.raises(RuntimeError, match="uniform duplicate copies"):
        repair.validate_repair_inputs(
            SPEC, "2026-09-07", rows, ledger_rows(), RAW,
            {"csv_sha256": SHA, "report_id": "r1", "report_type": SPEC.report_type,
             "report_date": "2026-09-07", "job_id": "j1"},
        )


def test_post_repair_validation_accepts_one_table_and_ledger_copy():
    rows = table_rows()[:2]
    ledgers = ledger_rows()[:1]
    result = repair.validate_repair_inputs(
        SPEC, "2026-09-07", rows, ledgers, RAW,
        {"csv_sha256": SHA, "report_id": "r1", "report_type": SPEC.report_type,
         "report_date": "2026-09-07", "job_id": "j1"},
        require_duplicates=False,
    )
    assert result.copy_count == 1
    assert result.ledger_rows == 1


def test_validation_rejects_archive_table_row_difference():
    different = RAW.replace(b",50,0.5", b",51,0.5")
    sha = hashlib.sha256(different).hexdigest()
    ledgers = [dict(r, content_sha256=sha, csv_bytes=len(different)) for r in ledger_rows()]
    with pytest.raises(RuntimeError, match="archive rows differ"):
        repair.validate_repair_inputs(
            SPEC, "2026-09-07", table_rows(), ledgers, different,
            {"csv_sha256": sha, "report_id": "r1", "report_type": SPEC.report_type,
             "report_date": "2026-09-07", "job_id": "j1"},
        )


def test_repair_transaction_checks_mutex_and_rebuilds_partition_and_ledger():
    sql = repair.build_repair_script("p.ds", SPEC, "_repair_work")
    assert sql.index("UPDATE `p.ds.pipeline_write_mutex_reporting`") < sql.index("DELETE FROM `p.ds.reporting_channel_reach_basic_a1`")
    assert "WHERE mutex_name = 'reporting'" in sql
    assert "ASSERT @@row_count = 1" in sql
    assert "BEGIN TRANSACTION" in sql and "ROLLBACK TRANSACTION" in sql
    assert "DELETE FROM `p.ds.reporting_ingest_ledger` WHERE report_id = @report_id" in sql
    assert "INSERT INTO `p.ds.reporting_ingest_ledger`" in sql


def test_archive_download_uses_stored_gzip_bytes():
    class Blob:
        metadata = {"csv_sha256": SHA}

        def download_as_bytes(self, raw_download=False):
            assert raw_download is True
            return gzip.compress(RAW)

    assert repair.download_archive_csv(Blob()) == RAW
