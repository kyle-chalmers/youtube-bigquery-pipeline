#!/usr/bin/env python3
"""Fail closed when a post-change production proof capture violates its contracts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


CLEAN_QUERY_OUTPUTS = (
    "analytics_multiple_load_sources",
    "incident_device_duplicate_rows",
    "incident_traffic_duplicate_rows",
    "ledger_duplicates",
    "ledger_table_mismatches",
    "loaded_generations",
    "reporting_multiple_report_ids",
    "reporting_native_grain_duplicates",
)

REQUIRED_QUERY_OUTPUTS = frozenset({
    "analytics_integrity",
    "analytics_multiple_load_sources",
    "analytics_partial_days",
    "analytics_partition_fingerprints",
    "analytics_refresh_archive_reconciliation",
    "column_partitioning_clustering",
    "cross_source_date_coverage",
    "incident_device_canonical_copies",
    "incident_device_duplicate_rows",
    "incident_device_rows",
    "incident_device_view_rows",
    "incident_ledger_rows",
    "incident_traffic_canonical_copies",
    "incident_traffic_duplicate_rows",
    "incident_traffic_rows",
    "incident_traffic_view_rows",
    "ledger_duplicates",
    "ledger_table_mismatches",
    "loaded_generations",
    "manifest",
    "pipeline_mutex",
    "pipeline_mutex_objects",
    "reconcile_subscribers",
    "reconcile_views_channel_day",
    "reconcile_views_video_day",
    "reporting_coverage_calendar",
    "reporting_generation_latency",
    "reporting_multiple_report_ids",
    "reporting_native_grain_duplicates",
    "reporting_partition_inventory",
    "reporting_table_counts",
    "table_metadata",
    "traffic_anonymization",
})

EXPECTED_FUNCTIONS = {
    "youtube-bigquery-pipeline": {
        "timeout": 540,
        "env": {
            "ANALYTICS_LOOKBACK_DAYS": "6",
            "GAP_LOOKBACK_DAYS": "21",
            "MAX_GAP_REPAIRS_PER_RUN": "5",
            "PIPELINE_TZ": "America/Phoenix",
            "REPORTING_ENABLED": "false",
            "RUN_LEASE_TTL_SECONDS": "2100",
        },
    },
    "youtube-reporting-ingest": {
        "timeout": 1500,
        "env": {
            "MAX_REPORTS_PER_RUN": "30",
            "REPORTING_ENABLED": "true",
            "REPORTING_RUNTIME_BUDGET_SECONDS": "1200",
            "REPORTING_STALE_DAYS": "4",
            "RUN_LEASE_TTL_SECONDS": "2100",
        },
    },
    "youtube-analytics-refresh": {
        "timeout": 1500,
        "env": {
            "ANALYTICS_LOOKBACK_DAYS": "6",
            "REFRESH_MIN_ROW_RATIO": "0.5",
            "REFRESH_TRAILING_DAYS": "30",
            "RUN_LEASE_TTL_SECONDS": "2100",
        },
    },
}

EXPECTED_SCHEDULERS = {
    "youtube-daily-snapshot": {
        "deadline": "600s", "retries": 3, "schedule": "10 0 * * *",
        "service": "youtube-bigquery-pipeline", "timezone": "America/Phoenix",
    },
    "youtube-reporting-daily": {
        "deadline": "1800s", "retries": 0, "schedule": "0 8,14 * * *",
        "service": "youtube-reporting-ingest", "timezone": "America/Phoenix",
    },
    "youtube-analytics-refresh-weekly": {
        "deadline": "1500s", "retries": 0, "schedule": "0 3 * * 0",
        "service": "youtube-analytics-refresh", "timezone": "America/Phoenix",
    },
}

EXPECTED_ALERT_SERVICES = {
    "youtube-analytics-failure": "youtube-bigquery-pipeline",
    "youtube-reporting-failure": "youtube-reporting-ingest",
    "youtube-reporting-stale": "youtube-reporting-ingest",
    "youtube-refresh-failure": "youtube-analytics-refresh",
}

def expected_config_outputs(resource_suffix: str) -> frozenset[str]:
    return frozenset(
        [f"configuration/function_{name}{resource_suffix}.json" for name in EXPECTED_FUNCTIONS]
        + [f"configuration/scheduler_{name}{resource_suffix}.json" for name in EXPECTED_SCHEDULERS]
        + [
            "configuration/monitoring_policies.json",
            "configuration/lock_bucket.json",
            "configuration/lock_bucket_iam.json",
            "configuration/lease_objects.json",
        ]
    )


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_file(capture_dir: Path, relative: str) -> Any:
    return json.loads((capture_dir / relative).read_text())


def _alert_filter(policy: dict[str, Any]) -> str:
    return "\n".join(
        condition.get("conditionMatchedLog", {}).get("filter", "")
        for condition in policy.get("conditions", [])
    )


def verify_configuration(capture_dir: Path, manifest: dict[str, Any]) -> list[str]:
    errors = []
    resource_suffix = str(manifest.get("resource_suffix", ""))
    config_items = manifest.get("configuration", [])
    config_outputs = {item.get("output") for item in config_items}
    missing = sorted(expected_config_outputs(resource_suffix) - config_outputs)
    if missing:
        errors.append(f"missing configuration captures: {missing}")
        return errors

    for item in config_items:
        if item.get("exit_code") != 0:
            errors.append(f"configuration command failed: {item.get('output')}")

    lock_bucket = _json_file(capture_dir, "configuration/lock_bucket.json")
    bucket_name = lock_bucket.get("name")
    if lock_bucket.get("public_access_prevention") != "enforced":
        errors.append("lock bucket public access prevention is not enforced")
    if lock_bucket.get("uniform_bucket_level_access") is not True:
        errors.append("lock bucket uniform access is not enabled")

    service_accounts = set()
    function_uris = {}
    function_env = {}
    for name, expected in EXPECTED_FUNCTIONS.items():
        full_name = f"{name}{resource_suffix}"
        function = _json_file(capture_dir, f"configuration/function_{full_name}.json")
        service = function.get("serviceConfig", {})
        function_uris[name] = str(service.get("uri", "")).rstrip("/")
        service_accounts.add(service.get("serviceAccountEmail"))
        if service.get("timeoutSeconds") != expected["timeout"]:
            errors.append(f"wrong function timeout: {name}")
        if service.get("maxInstanceCount") != 1:
            errors.append(f"wrong maximum instances: {name}")
        if service.get("maxInstanceRequestConcurrency") != 2:
            errors.append(f"wrong request concurrency: {name}")
        env = service.get("environmentVariables", {})
        function_env[name] = env
        if env.get("GCP_PROJECT") != manifest.get("project"):
            errors.append(f"wrong function project: {name}")
        if env.get("BQ_DATASET") != manifest.get("dataset"):
            errors.append(f"wrong function dataset: {name}")
        if env.get("PIPELINE_LOCK_BUCKET") != bucket_name:
            errors.append(f"wrong lock bucket environment variable: {name}")
        for variable, expected_value in expected["env"].items():
            if env.get(variable) != expected_value:
                errors.append(f"wrong function environment variable: {name}.{variable}")

    daily_env = function_env.get("youtube-bigquery-pipeline", {})
    reporting_env = function_env.get("youtube-reporting-ingest", {})
    channel_id = daily_env.get("YOUTUBE_CHANNEL_ID")
    expected_uploads = f"UU{channel_id[2:]}" if isinstance(channel_id, str) \
        and channel_id.startswith("UC") else None
    if not expected_uploads or daily_env.get("UPLOADS_PLAYLIST_ID") != expected_uploads:
        errors.append("daily uploads playlist does not match the channel")
    if not channel_id or reporting_env.get("YOUTUBE_CHANNEL_ID") != channel_id:
        errors.append("daily and Reporting channel IDs differ")
    expected_archive = f"{manifest.get('project')}-youtube-reporting-raw"
    if reporting_env.get("REPORTING_ARCHIVE_BUCKET") != expected_archive:
        errors.append("wrong Reporting archive bucket")

    for name, expected in EXPECTED_SCHEDULERS.items():
        full_name = f"{name}{resource_suffix}"
        scheduler = _json_file(capture_dir, f"configuration/scheduler_{full_name}.json")
        retries = scheduler.get("retryConfig", {}).get("retryCount", 0)
        target = scheduler.get("httpTarget", {})
        if scheduler.get("state") != "PAUSED":
            errors.append(f"production Scheduler is not paused: {name}")
        if scheduler.get("attemptDeadline") != expected["deadline"]:
            errors.append(f"wrong Scheduler deadline: {name}")
        if retries != expected["retries"]:
            errors.append(f"wrong Scheduler retries: {name}")
        if scheduler.get("schedule") != expected["schedule"]:
            errors.append(f"wrong Scheduler schedule: {name}")
        if scheduler.get("timeZone") != expected["timezone"]:
            errors.append(f"wrong Scheduler timezone: {name}")
        if str(target.get("uri", "")).rstrip("/") != function_uris[expected["service"]]:
            errors.append(f"wrong Scheduler target: {name}")

    policies = {
        policy.get("displayName"): policy
        for policy in _json_file(capture_dir, "configuration/monitoring_policies.json")
    }
    for name, service in EXPECTED_ALERT_SERVICES.items():
        full_name = f"{name}{resource_suffix}"
        full_service = f"{service}{resource_suffix}"
        policy = policies.get(full_name)
        if not policy:
            errors.append(f"missing environment alert: {full_name}")
            continue
        filter_text = _alert_filter(policy)
        if policy.get("severity") != "ERROR" or policy.get("enabled") is not True:
            errors.append(f"environment alert is not enabled ERROR: {full_name}")
        channels = policy.get("notificationChannels", [])
        if not isinstance(channels, list) or not any(isinstance(value, str) and value for value in channels):
            errors.append(f"environment alert has no notification channel: {full_name}")
        if f'resource.labels.service_name="{full_service}"' not in filter_text:
            errors.append(f"environment alert lacks exact service: {full_name}")
        if resource_suffix and f'resource.labels.service_name="{service}"' in filter_text:
            errors.append(f"staging alert can match production: {full_name}")
        if not resource_suffix and "-staging" in filter_text:
            errors.append(f"production alert can match staging: {full_name}")
    scheduler_policy_name = f"youtube-scheduler-failure{resource_suffix}"
    scheduler_policy = policies.get(scheduler_policy_name)
    if not scheduler_policy:
        errors.append(f"missing environment alert: {scheduler_policy_name}")
    else:
        scheduler_filter = _alert_filter(scheduler_policy)
        if scheduler_policy.get("severity") != "ERROR" or scheduler_policy.get("enabled") is not True:
            errors.append("production Scheduler alert is not enabled ERROR")
        channels = scheduler_policy.get("notificationChannels", [])
        if not isinstance(channels, list) or not any(isinstance(value, str) and value for value in channels):
            errors.append("environment Scheduler alert has no notification channel")
        for job in EXPECTED_SCHEDULERS:
            full_job = f"{job}{resource_suffix}"
            if f'resource.labels.job_id="{full_job}"' not in scheduler_filter:
                errors.append(f"environment Scheduler alert lacks exact job: {full_job}")
            if resource_suffix and f'resource.labels.job_id="{job}"' in scheduler_filter:
                errors.append(f"staging Scheduler alert can match production: {job}")
        if not resource_suffix and "-staging" in scheduler_filter:
            errors.append("production Scheduler alert can match staging")

    iam = _json_file(capture_dir, "configuration/lock_bucket_iam.json")
    object_users = {
        member
        for binding in iam.get("bindings", [])
        if binding.get("role") == "roles/storage.objectUser"
        for member in binding.get("members", [])
    }
    required_members = {f"serviceAccount:{account}" for account in service_accounts if account}
    if not required_members or not required_members.issubset(object_users):
        errors.append("lock bucket objectUser IAM lacks a function service account")

    lease_objects = _json_file(capture_dir, "configuration/lease_objects.json")
    active_objects = [
        item for item in lease_objects
        if item.get("type") not in (None, "unknown")
        or str(item.get("url", "")).rstrip("/") != f"gs://{bucket_name}"
    ]
    if active_objects:
        errors.append(f"lock bucket contains lease objects: {len(active_objects)}")
    return errors


def verify_capture(capture_dir: Path, baseline_dir: Path | None = None) -> dict[str, Any]:
    manifest = json.loads((capture_dir / "manifest.json").read_text())
    errors = []

    query_names = set(manifest.get("queries", {}))
    if query_names != REQUIRED_QUERY_OUTPUTS:
        errors.append(
            f"proof query set differs: missing={sorted(REQUIRED_QUERY_OUTPUTS - query_names)} "
            f"extra={sorted(query_names - REQUIRED_QUERY_OUTPUTS)}"
        )

    for name, query in manifest.get("queries", {}).items():
        path = capture_dir / query["output"]
        if not path.exists():
            errors.append(f"missing query output: {name}")
            continue
        rows = csv_rows(path)
        if len(rows) != query["rows"]:
            errors.append(f"query row count mismatch: {name}")
        if _file_sha(path) != query["sha256"]:
            errors.append(f"query file hash mismatch: {name}")

    errors.extend(verify_configuration(capture_dir, manifest))

    for archive in manifest.get("archives", []):
        if not archive.get("sha256_equal"):
            errors.append(f"archive sha mismatch: {archive.get('report_type')}")
        path = capture_dir / archive["csv"]
        if not path.exists() or _file_sha(path) != archive.get("calculated_sha256"):
            errors.append(f"archive file hash mismatch: {archive.get('report_type')}")

    for comparison in manifest.get("archive_reconciliation", []):
        for direction in ("archive_only", "bigquery_only"):
            detail = comparison.get(direction, {})
            path = capture_dir / str(detail.get("output", ""))
            if detail.get("rows") != 0:
                errors.append(
                    f"{direction} rows exist: {comparison.get('report_type')}={detail.get('rows')}"
                )
            if not path.exists() or _file_sha(path) != detail.get("sha256"):
                errors.append(
                    f"{direction} file hash mismatch: {comparison.get('report_type')}"
                )

    for name in CLEAN_QUERY_OUTPUTS:
        query = manifest.get("queries", {}).get(name)
        if query is None:
            errors.append(f"missing clean query: {name}")
        elif query.get("rows") != 0:
            errors.append(f"clean query returned rows: {name}={query.get('rows')}")

    analytics = csv_rows(capture_dir / "analytics_integrity.csv")
    if len(analytics) != 4:
        errors.append(f"analytics_integrity expected 4 tables, found {len(analytics)}")
    for row in analytics:
        if row.get("duplicate_groups") != "0":
            errors.append(f"analytics duplicates: {row.get('table_name')}")

    mutex = {
        row.get("mutex_name"): row.get("rows_for_mutex")
        for row in csv_rows(capture_dir / "pipeline_mutex.csv")
    }
    if mutex != {"analytics": "1", "reporting": "1"}:
        errors.append(f"mutex rows are not singleton domains: {mutex}")

    objects = {
        (row.get("table_name"), row.get("table_type"))
        for row in csv_rows(capture_dir / "pipeline_mutex_objects.csv")
    }
    expected_objects = {
        ("pipeline_write_mutex", "VIEW"),
        ("pipeline_write_mutex_analytics", "BASE TABLE"),
        ("pipeline_write_mutex_reporting", "BASE TABLE"),
    }
    if objects != expected_objects:
        errors.append(f"mutex objects differ: {sorted(objects)}")

    for name in ("incident_device_canonical_copies", "incident_traffic_canonical_copies"):
        rows = csv_rows(capture_dir / f"{name}.csv")
        if not rows:
            errors.append(f"canonical incident query is empty: {name}")
        if any(row.get("physical_copy_count") != "1" for row in rows):
            errors.append(f"canonical copy count is not one: {name}")

    expected_archive_types = {"channel_device_os_a3", "channel_traffic_source_a3"}
    archive_types = {archive.get("report_type") for archive in manifest.get("archives", [])}
    reconciliation_types = {
        comparison.get("report_type") for comparison in manifest.get("archive_reconciliation", [])
    }
    if archive_types != expected_archive_types:
        errors.append(f"archive set differs: {sorted(archive_types)}")
    if reconciliation_types != expected_archive_types:
        errors.append(f"archive reconciliation set differs: {sorted(reconciliation_types)}")
    canonical_outputs = {
        "channel_device_os_a3": "incident_device_canonical_copies",
        "channel_traffic_source_a3": "incident_traffic_canonical_copies",
    }
    for comparison in manifest.get("archive_reconciliation", []):
        report_type = comparison.get("report_type")
        archive_rows = comparison.get("archive_canonical_rows")
        bigquery_rows = comparison.get("bigquery_canonical_rows")
        query_name = canonical_outputs.get(report_type)
        query_rows = manifest.get("queries", {}).get(query_name, {}).get("rows") if query_name else None
        if not isinstance(archive_rows, int) or archive_rows <= 0:
            errors.append(f"archive canonical row count is empty: {report_type}")
        if archive_rows != bigquery_rows or archive_rows != query_rows:
            errors.append(
                f"archive, BigQuery and canonical row counts differ: {report_type}="
                f"{archive_rows}/{bigquery_rows}/{query_rows}"
            )

    if baseline_dir is not None:
        for filename in ("analytics_integrity.csv", "analytics_partition_fingerprints.csv"):
            if csv_rows(capture_dir / filename) != csv_rows(baseline_dir / filename):
                errors.append(f"Analytics baseline changed: {filename}")

    result = {
        "archive_comparisons": len(manifest.get("archive_reconciliation", [])),
        "errors": errors,
        "queries_verified": len(manifest.get("queries", {})),
        "status": "PASS" if not errors else "FAIL",
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path)
    args = parser.parse_args()
    result = verify_capture(args.capture_dir.resolve(), args.baseline_dir.resolve() if args.baseline_dir else None)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
