"""The staging gate reproduces the empty-partition race and verifies lease cleanup."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_standalone_python_gates_mint_a_short_lived_cli_token():
    for name in (
        "test_concurrent_staging.sh",
        "test_leases_staging.sh",
        "test_analytics_concurrent_staging.sh",
        "test_reporting_transaction_staging.sh",
        "test_cross_domain_mutex_staging.sh",
    ):
        text = (ROOT / "scripts" / name).read_text()
        assert 'export GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"' in text


def test_concurrency_gate_starts_from_an_empty_partition_and_expects_one_lease_loser():
    text = (ROOT / "scripts" / "test_concurrent_staging.sh").read_text()
    assert "DELETE FROM" in text
    assert "lease_held" in text
    assert "held_total" in text
    assert ".get('reason') == 'lease_held'" in text
    assert "manage_run_lease.py status" in text


def test_staging_verifier_runs_repair_rollback_and_full_checks():
    text = (ROOT / "scripts" / "verify_remediation_staging.sh").read_text()
    for required in (
        "test_concurrent_staging.sh",
        "repair_reporting_duplicates.py",
        "CREATE OR REPLACE TABLE",
        "verify_reporting.sh",
        "verify_views.sh",
        "generate_reporting_ddl.py --check",
        "test_leases_staging.sh",
        "test_analytics_concurrent_staging.sh",
        "test_reporting_transaction_staging.sh",
        "test_cross_domain_mutex_staging.sh",
        "test_transaction_rollback_staging.sh",
    ):
        assert required in text
    assert "youtube_analytics_staging" in text
    assert "youtube_analytics`" not in text
    assert "--format=csv" in text
    assert "$'analytics,1\\nreporting,1'" in text


def test_live_lease_gate_checks_held_expired_and_generation_mismatch():
    text = (ROOT / "scripts" / "test_leases_staging.sh").read_text()
    assert "lease_held" in text
    assert "expired" in text
    assert "wrong_generation" in text
    assert "manage_run_lease.py clear" in text


def test_analytics_race_uses_an_empty_staging_partition():
    text = (ROOT / "scripts" / "test_analytics_concurrent_staging.sh").read_text()
    assert "1900-01-01" in text
    assert "write_daily_video_analytics" in text
    assert "COUNT(*)" in text
    assert "native_keys" in text
    assert "rows[0].keys" not in text
    assert "DELETE FROM" in text


def test_reporting_transaction_race_bypasses_the_gcs_lease_on_an_empty_partition():
    text = (ROOT / "scripts" / "test_reporting_transaction_staging.sh").read_text()
    assert "StagedTransactionalReplacer" in text
    assert "AlreadyLoaded" in text
    assert "DELETE FROM" in text
    assert "PIPELINE_LOCK_BUCKET" not in text


def test_cross_domain_race_holds_different_mutex_partitions():
    text = (ROOT / "scripts" / "test_cross_domain_mutex_staging.sh").read_text()
    assert "pipeline_write_mutex_analytics" in text
    assert "pipeline_write_mutex_reporting" in text
    assert "for round_number in range(5)" in text
    assert "CROSS-DOMAIN MUTEX: PASS" in text


def test_rollback_gate_forces_a_failure_after_delete_and_compares_fingerprints():
    text = (ROOT / "scripts" / "test_transaction_rollback_staging.sh").read_text()
    assert "synthetic crash after delete" in text
    assert '"$output" == *"synthetic crash"* && "$output" == *"after delete"*' in text
    assert "ROLLBACK TRANSACTION" in text
    assert "FARM_FINGERPRINT" in text
    assert "1900-01-02" in text
