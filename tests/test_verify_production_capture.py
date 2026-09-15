"""Tests for fail-closed post-change proof verification."""

import csv
import hashlib
import importlib
import json


verify = importlib.import_module("verify_production_capture")


def write_csv(path, header, rows=()):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def config_capture(tmp_path, resource_suffix=""):
    dataset = "analytics_dataset"
    bucket = "pipeline-locks-prod"
    service_account = "pipeline@example.invalid"
    functions = {
        "youtube-bigquery-pipeline": (540, {
            "ANALYTICS_LOOKBACK_DAYS": "6",
            "GAP_LOOKBACK_DAYS": "21",
            "MAX_GAP_REPAIRS_PER_RUN": "5",
            "PIPELINE_TZ": "America/Phoenix",
            "REPORTING_ENABLED": "false",
            "RUN_LEASE_TTL_SECONDS": "2100",
        }),
        "youtube-reporting-ingest": (1500, {
            "MAX_REPORTS_PER_RUN": "30",
            "REPORTING_ENABLED": "true",
            "REPORTING_RUNTIME_BUDGET_SECONDS": "1200",
            "REPORTING_STALE_DAYS": "4",
            "RUN_LEASE_TTL_SECONDS": "2100",
        }),
        "youtube-analytics-refresh": (1500, {
            "ANALYTICS_LOOKBACK_DAYS": "6",
            "REFRESH_MIN_ROW_RATIO": "0.5",
            "REFRESH_TRAILING_DAYS": "30",
            "RUN_LEASE_TTL_SECONDS": "2100",
        }),
    }
    configuration = []
    for name, (timeout, expected_env) in functions.items():
        full_name = f"{name}{resource_suffix}"
        uri = f"https://{full_name}.example.invalid"
        env = {
            "BQ_DATASET": dataset,
            "GCP_PROJECT": "example-project",
            "PIPELINE_LOCK_BUCKET": bucket,
            **expected_env,
        }
        if name in ("youtube-bigquery-pipeline", "youtube-reporting-ingest"):
            env["YOUTUBE_CHANNEL_ID"] = "UCexample"
        if name == "youtube-bigquery-pipeline":
            env["UPLOADS_PLAYLIST_ID"] = "UUexample"
        if name == "youtube-reporting-ingest":
            env["REPORTING_ARCHIVE_BUCKET"] = "example-project-youtube-reporting-raw"
        relative = f"configuration/function_{full_name}.json"
        write_json(tmp_path / relative, {"serviceConfig": {
            "environmentVariables": env,
            "maxInstanceCount": 1,
            "maxInstanceRequestConcurrency": 2,
            "serviceAccountEmail": service_account,
            "timeoutSeconds": timeout,
            "uri": uri,
        }})
        configuration.append({"exit_code": 0, "output": relative})
    schedulers = {
        "youtube-daily-snapshot": (
            "600s", 3, "youtube-bigquery-pipeline", "10 0 * * *"
        ),
        "youtube-reporting-daily": (
            "1800s", 0, "youtube-reporting-ingest", "0 8,14 * * *"
        ),
        "youtube-analytics-refresh-weekly": (
            "1500s", 0, "youtube-analytics-refresh", "0 3 * * 0"
        ),
    }
    for name, (deadline, retries, service, schedule) in schedulers.items():
        full_name = f"{name}{resource_suffix}"
        relative = f"configuration/scheduler_{full_name}.json"
        write_json(tmp_path / relative, {
            "attemptDeadline": deadline,
            "httpTarget": {"uri": f"https://{service}{resource_suffix}.example.invalid/"},
            "retryConfig": {"retryCount": retries},
            "schedule": schedule,
            "state": "PAUSED",
            "timeZone": "America/Phoenix",
        })
        configuration.append({"exit_code": 0, "output": relative})
    policies = []
    for name, service in verify.EXPECTED_ALERT_SERVICES.items():
        policies.append({
            "conditions": [{"conditionMatchedLog": {
                "filter": f'resource.labels.service_name="{service}{resource_suffix}"'
            }}],
            "displayName": f"{name}{resource_suffix}",
            "enabled": True,
            "notificationChannels": ["projects/example-project/notificationChannels/1"],
            "severity": "ERROR",
        })
    policies.append({
        "conditions": [{"conditionMatchedLog": {"filter": " OR ".join(
            f'resource.labels.job_id="{name}{resource_suffix}"' for name in verify.EXPECTED_SCHEDULERS
        )}}],
        "displayName": f"youtube-scheduler-failure{resource_suffix}",
        "enabled": True,
        "notificationChannels": ["projects/example-project/notificationChannels/1"],
        "severity": "ERROR",
    })
    values = {
        "configuration/monitoring_policies.json": policies,
        "configuration/lock_bucket.json": {
            "name": bucket,
            "public_access_prevention": "enforced",
            "uniform_bucket_level_access": True,
        },
        "configuration/lock_bucket_iam.json": {"bindings": [{
            "members": [f"serviceAccount:{service_account}"],
            "role": "roles/storage.objectUser",
        }]},
        "configuration/lease_objects.json": [{"type": "unknown", "url": f"gs://{bucket}/"}],
    }
    for relative, value in values.items():
        write_json(tmp_path / relative, value)
        configuration.append({"exit_code": 0, "output": relative})
    return configuration


def minimal_capture(tmp_path, *, duplicate_query=None, resource_suffix=""):
    queries = {}
    for name in verify.REQUIRED_QUERY_OUTPUTS:
        path = tmp_path / f"{name}.csv"
        rows = [["bad"]] if name == duplicate_query else []
        header = ["value"]
        if name == "analytics_integrity":
            header = ["table_name", "duplicate_groups"]
            rows = [[table, "0"] for table in (
                "video_metadata", "daily_video_stats", "daily_video_analytics", "daily_traffic_sources"
            )]
        elif name == "analytics_partition_fingerprints":
            header = ["table_name", "partition_date", "physical_rows", "fingerprint"]
            rows = [["video_metadata", "2026-09-13", "1", "7"]]
        elif name == "pipeline_mutex":
            header = ["mutex_name", "rows_for_mutex"]
            rows = [["analytics", "1"], ["reporting", "1"]]
        elif name == "pipeline_mutex_objects":
            header = ["table_name", "table_type"]
            rows = [
                ["pipeline_write_mutex", "VIEW"],
                ["pipeline_write_mutex_analytics", "BASE TABLE"],
                ["pipeline_write_mutex_reporting", "BASE TABLE"],
            ]
        elif name in ("incident_device_canonical_copies", "incident_traffic_canonical_copies"):
            header = ["physical_copy_count"]
            rows = [["1"]]
        sha = write_csv(path, header, rows)
        queries[name] = {"output": path.name, "rows": len(rows), "sha256": sha}
    archives = []
    reconciliation = []
    for report_type in ("channel_device_os_a3", "channel_traffic_source_a3"):
        archive_path = tmp_path / "archives" / f"{report_type}.csv"
        archive_path.parent.mkdir(exist_ok=True)
        archive_sha = write_csv(archive_path, ["value"], [["row"]])
        archives.append({
            "calculated_sha256": archive_sha,
            "csv": str(archive_path.relative_to(tmp_path)),
            "report_type": report_type,
            "sha256_equal": True,
        })
        directions = {}
        for direction in ("archive_only", "bigquery_only"):
            path = tmp_path / f"{direction}_{report_type}.csv"
            sha = write_csv(path, ["value"])
            directions[direction] = {"output": path.name, "rows": 0, "sha256": sha}
        reconciliation.append({
            "archive_canonical_rows": 1,
            "bigquery_canonical_rows": 1,
            "report_type": report_type,
            **directions,
        })
    (tmp_path / "manifest.json").write_text(json.dumps({
        "archive_reconciliation": reconciliation,
        "archives": archives,
        "configuration": config_capture(tmp_path, resource_suffix),
        "dataset": "analytics_dataset",
        "project": "example-project",
        "queries": queries,
        "resource_suffix": resource_suffix,
    }))


def test_clean_capture_passes_and_matches_analytics_baseline(tmp_path):
    capture = tmp_path / "capture"
    baseline = tmp_path / "baseline"
    capture.mkdir()
    baseline.mkdir()
    minimal_capture(capture)
    minimal_capture(baseline)
    result = verify.verify_capture(capture, baseline)
    assert result["status"] == "PASS"


def test_staging_resource_suffix_uses_exact_staging_config(tmp_path):
    minimal_capture(tmp_path, resource_suffix="-staging")
    result = verify.verify_capture(tmp_path)
    assert result["status"] == "PASS"


def test_nonempty_integrity_query_fails(tmp_path):
    minimal_capture(tmp_path, duplicate_query="ledger_duplicates")
    result = verify.verify_capture(tmp_path)
    assert result["status"] == "FAIL"
    assert "clean query returned rows: ledger_duplicates=1" in result["errors"]


def test_analytics_fingerprint_change_fails(tmp_path):
    capture = tmp_path / "capture"
    baseline = tmp_path / "baseline"
    capture.mkdir()
    baseline.mkdir()
    minimal_capture(capture)
    minimal_capture(baseline)
    write_csv(
        capture / "analytics_partition_fingerprints.csv",
        ["table_name", "partition_date", "physical_rows", "fingerprint"],
        [["video_metadata", "2026-09-13", "2", "9"]],
    )
    result = verify.verify_capture(capture, baseline)
    assert "Analytics baseline changed: analytics_partition_fingerprints.csv" in result["errors"]


def test_wrong_runtime_configuration_fails(tmp_path):
    minimal_capture(tmp_path)
    path = tmp_path / "configuration/function_youtube-reporting-ingest.json"
    value = json.loads(path.read_text())
    value["serviceConfig"]["timeoutSeconds"] = 600
    write_json(path, value)
    result = verify.verify_capture(tmp_path)
    assert "wrong function timeout: youtube-reporting-ingest" in result["errors"]


def test_wrong_function_environment_fails(tmp_path):
    minimal_capture(tmp_path)
    path = tmp_path / "configuration/function_youtube-analytics-refresh.json"
    value = json.loads(path.read_text())
    value["serviceConfig"]["environmentVariables"]["REFRESH_TRAILING_DAYS"] = "7"
    write_json(path, value)
    result = verify.verify_capture(tmp_path)
    assert (
        "wrong function environment variable: youtube-analytics-refresh.REFRESH_TRAILING_DAYS"
        in result["errors"]
    )


def test_wrong_scheduler_schedule_and_timezone_fail(tmp_path):
    minimal_capture(tmp_path)
    path = tmp_path / "configuration/scheduler_youtube-reporting-daily.json"
    value = json.loads(path.read_text())
    value["schedule"] = "0 9 * * *"
    value["timeZone"] = "UTC"
    write_json(path, value)
    result = verify.verify_capture(tmp_path)
    assert "wrong Scheduler schedule: youtube-reporting-daily" in result["errors"]
    assert "wrong Scheduler timezone: youtube-reporting-daily" in result["errors"]


def test_channel_and_archive_environment_drift_fails(tmp_path):
    minimal_capture(tmp_path)
    daily_path = tmp_path / "configuration/function_youtube-bigquery-pipeline.json"
    daily = json.loads(daily_path.read_text())
    daily["serviceConfig"]["environmentVariables"]["UPLOADS_PLAYLIST_ID"] = "wrong"
    write_json(daily_path, daily)
    reporting_path = tmp_path / "configuration/function_youtube-reporting-ingest.json"
    reporting = json.loads(reporting_path.read_text())
    reporting["serviceConfig"]["environmentVariables"]["YOUTUBE_CHANNEL_ID"] = "UCother"
    reporting["serviceConfig"]["environmentVariables"]["REPORTING_ARCHIVE_BUCKET"] = "wrong"
    write_json(reporting_path, reporting)
    result = verify.verify_capture(tmp_path)
    assert "daily uploads playlist does not match the channel" in result["errors"]
    assert "daily and Reporting channel IDs differ" in result["errors"]
    assert "wrong Reporting archive bucket" in result["errors"]


def test_alert_without_notification_channel_fails(tmp_path):
    minimal_capture(tmp_path)
    path = tmp_path / "configuration/monitoring_policies.json"
    policies = json.loads(path.read_text())
    policies[0]["notificationChannels"] = []
    write_json(path, policies)
    result = verify.verify_capture(tmp_path)
    assert any("alert has no notification channel" in error for error in result["errors"])


def test_missing_archive_and_empty_canonical_rows_fail(tmp_path):
    minimal_capture(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["archives"] = []
    manifest["queries"]["incident_device_canonical_copies"]["rows"] = 0
    path = tmp_path / "incident_device_canonical_copies.csv"
    manifest["queries"]["incident_device_canonical_copies"]["sha256"] = write_csv(
        path, ["physical_copy_count"]
    )
    manifest_path.write_text(json.dumps(manifest))
    result = verify.verify_capture(tmp_path)
    assert any(error.startswith("archive set differs") for error in result["errors"])
    assert "canonical incident query is empty: incident_device_canonical_copies" in result["errors"]
