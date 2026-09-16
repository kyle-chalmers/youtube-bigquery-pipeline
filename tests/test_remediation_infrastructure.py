"""Deployment and monitoring scripts pin the runtime safeguards."""

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def source(name: str) -> str:
    return (ROOT / "setup" / name).read_text()


def run_with_fake_gcloud(tmp_path, script_name: str, **overrides):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GCP_PROJECT": "test-project",
        "GCP_REGION": "us-central1",
        "YOUTUBE_CHANNEL_ID": "UCtestchannel000000000000",
        "BQ_DATASET": "youtube_analytics",
        "PIPELINE_LOCK_BUCKET": "wrong-lock-bucket",
        "REPORTING_ENABLED": "true",
        "REFRESH_TIMEOUT": "1500s",
        "ANALYTICS_LOOKBACK_DAYS": "6",
        "RUN_LEASE_TTL_SECONDS": "2100",
        **overrides,
    }
    return subprocess.run(
        ["bash", str(ROOT / "setup" / script_name)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_all_writer_deploys_require_the_lock_bucket_and_one_instance():
    for name in ("4_deploy_function.sh", "9_deploy_reporting_function.sh", "12_deploy_refresh_function.sh"):
        text = source(name)
        assert "PIPELINE_LOCK_BUCKET" in text
        assert "--max-instances=1" in text
        assert "--concurrency=2" in text
        assert "--cpu=1" in text
        assert "RUN_LEASE_TTL_SECONDS" in text


@pytest.mark.parametrize(
    "script_name",
    ("4_deploy_function.sh", "9_deploy_reporting_function.sh", "12_deploy_refresh_function.sh"),
)
def test_production_deploys_reject_a_nonproduction_lock_bucket(tmp_path, script_name):
    result = run_with_fake_gcloud(tmp_path, script_name)
    assert result.returncode != 0
    assert "production lock bucket" in result.stderr


@pytest.mark.parametrize(
    "script_name,function_name",
    (
        ("4_deploy_function.sh", "youtube-bigquery-pipeline-staging"),
        ("9_deploy_reporting_function.sh", "youtube-reporting-ingest-staging"),
        ("12_deploy_refresh_function.sh", "youtube-analytics-refresh-staging"),
    ),
)
def test_staging_deploys_reject_the_production_lock_bucket(tmp_path, script_name, function_name):
    result = run_with_fake_gcloud(
        tmp_path,
        script_name,
        FUNCTION_NAME=function_name,
        BQ_DATASET="youtube_analytics_staging",
        PIPELINE_LOCK_BUCKET="test-project-youtube-pipeline-locks-prod",
    )
    assert result.returncode != 0
    assert "staging lock bucket" in result.stderr


@pytest.mark.parametrize(
    "script_name",
    ("4_deploy_function.sh", "9_deploy_reporting_function.sh", "12_deploy_refresh_function.sh"),
)
def test_writer_deploys_reject_a_lease_ttl_without_release_margin(tmp_path, script_name):
    result = run_with_fake_gcloud(
        tmp_path,
        script_name,
        PIPELINE_LOCK_BUCKET="test-project-youtube-pipeline-locks-prod",
        RUN_LEASE_TTL_SECONDS="700",
    )
    assert result.returncode != 0
    assert "at least 300 seconds" in result.stderr


def test_staging_policy_suffix_rejects_production_resource_names(tmp_path):
    result = run_with_fake_gcloud(
        tmp_path,
        "6_setup_monitoring.sh",
        ALERT_EMAIL="pipeline@example.invalid",
        POLICY_SUFFIX="-staging",
        FUNCTION_NAME="youtube-bigquery-pipeline",
        REPORTING_FUNCTION_NAME="youtube-reporting-ingest",
        REFRESH_FUNCTION_NAME="youtube-analytics-refresh",
        SCHEDULER_JOB_IDS="youtube-daily-snapshot,youtube-reporting-daily,youtube-analytics-refresh-weekly",
    )
    assert result.returncode != 0
    assert "staging policy" in result.stderr


def test_reporting_deploy_pins_timeout_and_work_budget():
    text = source("9_deploy_reporting_function.sh")
    assert "--timeout=1500s" in text
    assert 'REPORTING_RUNTIME_BUDGET_SECONDS="${REPORTING_RUNTIME_BUDGET_SECONDS:-1200}"' in text


def test_scheduler_uses_safe_reporting_defaults():
    text = source("5_create_scheduler.sh")
    assert 'youtube-reporting-daily' in text
    assert 'ATTEMPT_DEADLINE="${ATTEMPT_DEADLINE:-1800s}"' in text
    assert 'MAX_RETRY_ATTEMPTS="${MAX_RETRY_ATTEMPTS:-0}"' in text
    assert '"$JOB_NAME" == *"analytics-refresh"*' in text
    assert 'ATTEMPT_DEADLINE="${ATTEMPT_DEADLINE:-1500s}"' in text


def test_monitoring_has_error_severity_and_explicit_scheduler_ids():
    text = source("6_setup_monitoring.sh")
    assert '"severity": "ERROR"' in text
    assert "SCHEDULER_JOB_IDS" in text
    scheduler_call = text.split('upsert_policy "youtube-scheduler-failure', 1)[1]
    assert "scheduler_filter" in scheduler_call
    assert 'resource.type=\\\"cloud_scheduler_job\\\" AND severity>=ERROR"' not in scheduler_call


def test_lock_bucket_script_uses_bucket_scoped_object_user_only():
    text = source("13_setup_run_locks.sh")
    assert "roles/storage.objectUser" in text
    assert "gcloud storage buckets add-iam-policy-binding" in text
    assert "gcloud projects add-iam-policy-binding" not in text
    assert "--pap" in text
    assert "uniform-bucket-level-access" in text
