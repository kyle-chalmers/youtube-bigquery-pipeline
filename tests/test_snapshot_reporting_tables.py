"""Tests for the all-table Reporting recovery snapshots."""

import importlib
from pathlib import Path

import pytest

from report_specs import LEDGER_TABLE, SPECS


snapshots = importlib.import_module("snapshot_reporting_tables")


def test_snapshot_scope_is_every_reporting_table_plus_ledger():
    expected = {spec.table for spec in SPECS.values()} | {LEDGER_TABLE}
    assert set(snapshots.REPORTING_TABLES) == expected
    assert len(snapshots.REPORTING_TABLES) == 20


def test_create_and_restore_sql_target_exact_snapshot():
    create = snapshots.create_sql("p.ds", "reporting_x", "before", 30)
    restore = snapshots.restore_sql("p.ds", "reporting_x", "before")
    assert "CREATE SNAPSHOT TABLE `p.ds.reporting_x__before`" in create
    assert "CLONE `p.ds.reporting_x`" in create
    assert "INTERVAL 30 DAY" in create
    assert restore == (
        "CREATE OR REPLACE TABLE `p.ds.reporting_x` "
        "CLONE `p.ds.reporting_x__before`"
    )


def test_mutating_snapshot_actions_use_reporting_writer_lease():
    source = Path(snapshots.__file__).read_text()
    assert "manual_writer_lease" in source
    assert 'domain="reporting-writer"' in source
    assert "dry_run=args.action == \"plan\"" in source


def test_restore_preflight_reads_every_snapshot_before_writes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshots,
        "_fingerprint",
        lambda client, dataset_ref, table: calls.append(table) or f"fingerprint:{table}",
    )
    result = snapshots.preflight_restore_fingerprints(object(), "p.ds", "before")
    expected = [snapshots.snapshot_name(table, "before") for table in snapshots.REPORTING_TABLES]
    assert calls == expected
    assert set(result) == set(snapshots.REPORTING_TABLES)


def test_fingerprint_sql_counts_and_hashes_complete_rows():
    sql = snapshots.fingerprint_sql("p.ds", "reporting_x")
    assert "COUNT(*)" in sql
    assert "BIT_XOR(row_hash)" in sql
    assert "SUM(CAST(row_hash AS BIGNUMERIC))" in sql
    assert "FARM_FINGERPRINT(TO_JSON_STRING(row_value)) AS row_hash" in sql
    assert "FROM `p.ds.reporting_x` AS row_value" in sql


@pytest.mark.parametrize("prefix", ("../bad", "bad-name", "", "bad prefix"))
def test_snapshot_prefix_rejects_unsafe_names(prefix):
    with pytest.raises(ValueError):
        snapshots.validate_prefix(prefix)
