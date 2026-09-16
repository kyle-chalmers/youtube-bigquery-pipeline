#!/usr/bin/env bash
set -euo pipefail

# Bypass the GCS leases and start both transaction domains together. BigQuery detects
# write conflicts by table, so the domains use separate physical mutex tables.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
PY="${PYTHON:-.venv/bin/python}"
[[ "$DS" == *staging* && "$DS" != "youtube_analytics" ]] || { echo "refusing non-staging dataset" >&2; exit 1; }
export GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"

PYTHONPATH=cloud_function "$PY" - "$PROJECT_ID" "$DS" "$REGION" <<'PY'
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from google.cloud import bigquery

from gcloud_credentials import credentials_from_environment

project, dataset, region = sys.argv[1:]
tables = {
    "analytics": "pipeline_write_mutex_analytics",
    "reporting": "pipeline_write_mutex_reporting",
}


def run(domain, barrier):
    client = bigquery.Client(project=project, credentials=credentials_from_environment(), location=region)
    barrier.wait()
    table = tables[domain]
    sql = f"""
BEGIN TRANSACTION;
UPDATE `{project}.{dataset}.{table}`
SET touched_at=CURRENT_TIMESTAMP()
WHERE mutex_name='{domain}';
ASSERT @@row_count=1 AS 'mutex singleton required';
COMMIT TRANSACTION;
"""
    client.query(sql).result()
    return domain


for round_number in range(5):
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda domain: run(domain, barrier), ("analytics", "reporting")))
    if results != ["analytics", "reporting"]:
        raise SystemExit(f"FAIL: round={round_number} cross-domain outcomes={results}")
print("STAGING CROSS-DOMAIN MUTEX: PASS rounds=5 independent_tables=2")
PY
