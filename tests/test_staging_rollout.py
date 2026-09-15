"""Staging rollout provisions only staging-named resources and leaves jobs paused."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_rollout_deploys_all_three_writers_and_pauses_three_staging_jobs():
    text = (ROOT / "setup" / "14_rollout_staging.sh").read_text()
    for function in (
        "youtube-bigquery-pipeline-staging",
        "youtube-reporting-ingest-staging",
        "youtube-analytics-refresh-staging",
    ):
        assert function in text
    for job in (
        "youtube-daily-snapshot-staging",
        "youtube-reporting-daily-staging",
        "youtube-analytics-refresh-weekly-staging",
    ):
        assert job in text
    assert text.count("gcloud scheduler jobs pause") >= 1
    assert "13_setup_run_locks.sh" in text
    assert "6_setup_monitoring.sh" in text
    assert "youtube_analytics_staging" in text
    assert "CONFIGURE_ARCHIVE_IAM=false" in text
    assert 'ANALYTICS_LOOKBACK_DAYS:-5' not in text
    assert "serviceConfig.environmentVariables.ANALYTICS_LOOKBACK_DAYS" in text
    assert 'youtube-daily-snapshot-staging "10 0 * * *" 600s 3' in text
    assert 'youtube-reporting-daily-staging "0 8,14 * * *" 1800s 0' in text
    assert 'youtube-analytics-refresh-weekly-staging "0 3 * * 0" "$REFRESH_TIMEOUT" 0' in text


def test_alert_scope_verifier_rejects_any_production_match():
    text = (ROOT / "scripts" / "verify_staging_alert_scope.py").read_text()
    assert "youtube-scheduler-failure-staging" in text
    assert "youtube-reporting-failure-staging" in text
    assert "youtube-reporting-daily-staging" in text
    assert 'resource.labels.job_id="{production}"' in text
    assert "severity" in text


def test_staging_copy_lists_every_object_and_checks_each_reporting_table():
    text = (ROOT / "setup" / "8_create_staging.sh").read_text()
    assert "--max_results=1000" in text
    assert "reporting_tables_found" in text
    assert "reporting_tables_copied" in text
    assert "src=$src_rows staging=$dst_rows" in text
