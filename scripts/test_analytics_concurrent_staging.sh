#!/usr/bin/env bash
set -euo pipefail

# Bypass the GCS lease and race two Analytics replacement transactions directly. The
# synthetic 1900-01-01 staging partition starts empty and is removed after the assertion.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
PY="${PYTHON:-.venv/bin/python}"
TEST_DATE="${ANALYTICS_TEST_DATE:-1900-01-01}"
[[ "$DS" == *staging* && "$DS" != "youtube_analytics" ]] || { echo "refusing non-staging dataset" >&2; exit 1; }
export GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"

PYTHONPATH=cloud_function "$PY" - "$PROJECT_ID" "$DS" "$TEST_DATE" <<'PY'
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from google.cloud import bigquery
from google.oauth2.credentials import Credentials
from bigquery_writer import BigQueryWriter

project, dataset, date_text = sys.argv[1:]
activity_date = date.fromisoformat(date_text)
table = f"{project}.{dataset}.daily_video_analytics"
credentials = Credentials(token=__import__("os").environ["GCLOUD_ACCESS_TOKEN"])
client = bigquery.Client(project=project, credentials=credentials)
cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", date_text)])
client.query(f"DELETE FROM `{table}` WHERE activity_date=@d", job_config=cfg).result()
barrier = threading.Barrier(2)

def run(label):
    writer = BigQueryWriter(project_id=project, dataset_id=dataset, credentials=credentials)
    row = {
        "video_id": "__analytics_mutex_test__",
        "estimated_minutes_watched": 1.0,
        "average_view_duration_seconds": 60.0,
        "average_view_percentage": 100.0,
        "impressions": None,
        "impression_ctr": None,
        "subscribers_gained": 0,
        "subscribers_lost": 0,
        "shares": 0,
        "annotation_click_through_rate": None,
        "card_click_rate": None,
    }
    barrier.wait()
    return writer.write_daily_video_analytics([row], activity_date, activity_date, label)

try:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("analytics_race_a", "analytics_race_b")))
    rows = list(client.query(
        f"SELECT COUNT(*) AS n, COUNT(DISTINCT video_id) AS native_keys FROM `{table}` WHERE activity_date=@d",
        job_config=cfg,
    ).result())
    if results != [1, 1] or rows[0].n != 1 or rows[0].native_keys != 1:
        raise SystemExit(
            f"FAIL: results={results} physical_rows={rows[0].n} native_keys={rows[0].native_keys}"
        )
    print(f"STAGING ANALYTICS TRANSACTION RACE: PASS date={date_text} results={results} final_rows=1")
finally:
    client.query(f"DELETE FROM `{table}` WHERE activity_date=@d", job_config=cfg).result()
PY
