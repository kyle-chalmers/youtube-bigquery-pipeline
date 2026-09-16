#!/usr/bin/env bash
set -euo pipefail

# Bypass the GCS writer lease and race two Reporting transactions directly against an
# empty staging partition. The mutex and loaded-report assertions must leave one copy.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
RTYPE="${REPORT_TYPE:-channel_reach_basic_a1}"
PY="${PYTHON:-.venv/bin/python}"
[[ "$DS" == *staging* && "$DS" != "youtube_analytics" ]] || { echo "refusing non-staging dataset" >&2; exit 1; }
export GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"

PYTHONPATH=cloud_function "$PY" - "$PROJECT_ID" "$DS" "$RTYPE" <<'PY'
import copy
import gzip
import hashlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from google.cloud import bigquery, storage
from google.oauth2.credentials import Credentials
from partition_replacer import AlreadyLoaded, StagedTransactionalReplacer
from report_specs import SPECS
from reporting_parser import parse_report

project, dataset, report_type = sys.argv[1:]
spec = SPECS[report_type]
dataset_ref = f"{project}.{dataset}"
credentials = Credentials(token=__import__("os").environ["GCLOUD_ACCESS_TOKEN"])
client = bigquery.Client(project=project, credentials=credentials)
params = [bigquery.ScalarQueryParameter("report_type", "STRING", report_type)]
cfg = bigquery.QueryJobConfig(query_parameters=params)
ledger = list(client.query(
    f"SELECT * FROM `{dataset_ref}.reporting_ingest_ledger` WHERE report_type=@report_type "
    "AND status='loaded' AND row_count > 0 QUALIFY COUNT(*) OVER (PARTITION BY report_id)=1 "
    "ORDER BY report_date DESC LIMIT 1", job_config=cfg).result())
if not ledger:
    raise SystemExit(f"no singular loaded {report_type} report available for the staging race")
entry = dict(ledger[0])
uri = urlparse(entry["gcs_uri"])
blob = storage.Client(project=project, credentials=credentials).bucket(uri.netloc).blob(uri.path.lstrip("/"))
raw = gzip.decompress(blob.download_as_bytes(raw_download=True))
rows = parse_report(raw, spec)
if not rows:
    raise SystemExit("selected report is header-only")
report_date = str(entry["report_date"])
date_cfg = bigquery.QueryJobConfig(query_parameters=[
    bigquery.ScalarQueryParameter("report_date", "DATE", report_date),
    bigquery.ScalarQueryParameter("report_id", "STRING", entry["report_id"]),
])
client.query(f"DELETE FROM `{dataset_ref}.{spec.table}` WHERE report_date=@report_date", job_config=date_cfg).result()
client.query(
    f"UPDATE `{dataset_ref}.reporting_ingest_ledger` SET status='failed', error='direct transaction race' "
    "WHERE report_id=@report_id", job_config=date_cfg).result()

barrier = threading.Barrier(2)
provenance = {
    "report_id": entry["report_id"], "job_id": entry["job_id"],
    "report_create_time": entry["report_create_time"].isoformat(),
    "load_source": "direct_transaction_race", "csv_bytes": len(raw),
    "content_sha256": hashlib.sha256(raw).hexdigest(), "gcs_uri": entry["gcs_uri"],
}
channel_ids = {row["channel_id"] for row in rows}
if len(channel_ids) != 1:
    raise SystemExit("selected archive does not contain exactly one channel id")

def run(_):
    replacer = StagedTransactionalReplacer(bigquery.Client(project=project, credentials=credentials), dataset_ref, next(iter(channel_ids)))
    barrier.wait()
    try:
        return replacer.replace_partition(spec, copy.deepcopy(rows), provenance)
    except AlreadyLoaded:
        return "already_loaded"

with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(run, (1, 2)))
result = list(client.query(
    f"SELECT COUNT(*) AS physical_rows, COUNT(DISTINCT report_id) AS report_ids FROM `{dataset_ref}.{spec.table}` "
    "WHERE report_date=@report_date", job_config=date_cfg).result())[0]
ledger_count = list(client.query(
    f"SELECT COUNT(*) AS n, COUNTIF(status='loaded') AS loaded FROM `{dataset_ref}.reporting_ingest_ledger` "
    "WHERE report_id=@report_id", job_config=date_cfg).result())[0]
if sorted(outcomes, key=str) != sorted([len(rows), "already_loaded"], key=str):
    raise SystemExit(f"FAIL: transaction outcomes={outcomes}")
if result.physical_rows != len(rows) or result.report_ids != 1 or ledger_count.n != 1 or ledger_count.loaded != 1:
    raise SystemExit(f"FAIL: rows={result.physical_rows} ids={result.report_ids} ledger={dict(ledger_count)}")
print(f"STAGING REPORTING TRANSACTION RACE: PASS date={report_date} outcomes={outcomes} final_rows={len(rows)}")
PY
