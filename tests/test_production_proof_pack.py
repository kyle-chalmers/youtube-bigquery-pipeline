"""The committed proof pack is generated from the Reporting schema registry."""

import importlib
from pathlib import Path

from report_specs import SPECS


generator = importlib.import_module("generate_reporting_ddl")
ROOT = Path(__file__).resolve().parent.parent


def test_proof_sql_is_generated_and_has_no_output_limits():
    rendered = generator.render_proof()
    assert (ROOT / "sql" / "verification" / "production_proof.sql").read_text() == rendered
    assert " LIMIT " not in rendered.upper()
    for marker in ("${PROJECT_ID}", "${BQ_DATASET}", "${AFFECTED_DATE}"):
        assert marker in rendered
    assert " AS rows," not in rendered


def test_proof_sql_covers_all_reporting_specs_at_native_grain():
    rendered = generator.render_proof()
    for spec in SPECS.values():
        assert f"`${{PROJECT_ID}}.${{BQ_DATASET}}.{spec.table}`" in rendered
        assert ", ".join(spec.grain_columns) in rendered
    assert "-- --reporting_native_grain_duplicates" in rendered
    assert "-- --reporting_partition_inventory" in rendered


def test_proof_sql_contains_incident_rows_analytics_fingerprints_and_mutex():
    rendered = generator.render_proof()
    for name in (
        "incident_device_rows", "incident_traffic_rows", "incident_ledger_rows",
        "incident_device_view_rows", "incident_traffic_view_rows", "analytics_integrity",
        "pipeline_mutex_objects", "pipeline_mutex", "ledger_duplicates", "loaded_generations",
        "reconcile_views_video_day", "reconcile_views_channel_day", "reconcile_subscribers",
        "cross_source_date_coverage", "analytics_partial_days", "reporting_coverage_calendar",
        "reporting_generation_latency", "traffic_anonymization",
        "analytics_multiple_load_sources", "analytics_refresh_archive_reconciliation",
    ):
        assert f"-- --{name}" in rendered
    assert "SELECT *" in rendered
    assert "BIT_XOR(FARM_FINGERPRINT" in rendered
    assert "IF EXISTS" in rendered
    assert "missing_before_deployment" in rendered


def test_analytics_partition_fingerprint_query_has_one_source_per_union_branch():
    rendered = generator.render_proof()
    duplicated = (
        "FROM `${PROJECT_ID}.${BQ_DATASET}.daily_video_analytics` t GROUP BY activity_date\n"
        "FROM `${PROJECT_ID}.${BQ_DATASET}.daily_video_analytics` t GROUP BY activity_date"
    )
    assert duplicated not in rendered


def test_capture_tool_dry_runs_then_writes_untruncated_csv_and_archive_files():
    text = (ROOT / "setup" / "capture_production_proof.py").read_text()
    assert "dry_run=True" in text
    assert "csv.writer" in text
    assert ".internal" in text
    assert "download_as_bytes(raw_download=True)" in text
    assert "capture_archive_reconciliation" in text
    assert "archive_only" in text and "bigquery_only" in text
    assert "gcloud" in text
    assert "dry-run failed" in text
    assert "configuration capture failed" in text


def test_capture_parser_finds_every_generated_query_block():
    capture = importlib.import_module("capture_production_proof")
    rendered = generator.render_proof()
    names = [name for name, _ in capture.parse_blocks(rendered)]
    assert names[0] == "manifest"
    assert names[-1] == "traffic_anonymization"
    assert len(names) >= 30
