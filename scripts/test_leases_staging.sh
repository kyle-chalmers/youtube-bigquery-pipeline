#!/usr/bin/env bash
set -euo pipefail

# Exercise live, expired and generation-mismatched leases against the staging Reporting
# function. Synthetic leases are cleared only by their exact generation.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
FN="${REPORTING_FUNCTION_NAME:-youtube-reporting-ingest-staging}"
BUCKET="${PIPELINE_LOCK_BUCKET:?set the staging lock bucket}"
PY="${PYTHON:-.venv/bin/python}"
[[ "$DS" == *staging* && "$FN" == *staging* && "$BUCKET" == *staging* ]] || { echo "refusing non-staging target" >&2; exit 1; }
export GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"

URL=$(gcloud functions describe "$FN" --region="$REGION" --gen2 --project="$PROJECT_ID" --format='value(serviceConfig.uri)')
TOKEN=$(gcloud auth print-identity-token)
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

make_lease() {
    local offset="$1" ttl="$2"
    PYTHONPATH=cloud_function "$PY" - "$PROJECT_ID" "$BUCKET" "$DS" "$offset" "$ttl" <<'PY'
import sys
from datetime import datetime, timedelta, timezone
from google.cloud import storage
from google.oauth2.credentials import Credentials
from run_lease import RunLease
project, bucket_name, dataset, offset, ttl = sys.argv[1:]
credentials = Credentials(token=__import__("os").environ["GCLOUD_ACCESS_TOKEN"])
bucket = storage.Client(project=project, credentials=credentials).bucket(bucket_name)
lease = RunLease(bucket, dataset=dataset, domain="reporting-writer",
                 owner={"entrypoint": "staging_lease_test", "run_id": "synthetic"},
                 ttl_seconds=int(ttl),
                 now=lambda: datetime.now(timezone.utc) + timedelta(seconds=int(offset)))
lease.acquire()
print(lease.generation)
PY
}

invoke() {
    local name="$1"
    curl -sS -X POST -H "Authorization: bearer $TOKEN" -o "$OUT/$name.json" -w '%{http_code}' "$URL"
}

echo "Checking a live lease returns a successful no-write response..."
generation=$(make_lease 0 30)
status=$(invoke live)
reason=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("reason"))' "$OUT/live.json")
[[ "$status" == "200" && "$reason" == "lease_held" ]] || { echo "FAIL: live lease response status=$status reason=$reason" >&2; exit 1; }

wrong_generation=$((generation + 1))
set +e
wrong_output=$(GCP_PROJECT="$PROJECT_ID" "$PY" setup/manage_run_lease.py clear --bucket "$BUCKET" --dataset "$DS" \
    --domain reporting-writer --generation "$wrong_generation" --confirm-dataset "$DS" \
    --confirm-no-active-writer 2>&1)
wrong_status=$?
set -e
[[ $wrong_status -ne 0 && "$wrong_output" == *"current generation $generation"* ]] || { echo "FAIL: generation mismatch was not refused" >&2; exit 1; }

set +e
live_output=$(GCP_PROJECT="$PROJECT_ID" "$PY" setup/manage_run_lease.py clear --bucket "$BUCKET" --dataset "$DS" \
    --domain reporting-writer --generation "$generation" --confirm-dataset "$DS" \
    --confirm-no-active-writer 2>&1)
live_status=$?
set -e
[[ $live_status -ne 0 && "$live_output" == *"live unexpired lease"* ]] || { echo "FAIL: live lease clear was not refused" >&2; exit 1; }

echo "Waiting for the 30-second synthetic lease to expire before exact-generation cleanup..."
sleep 31
GCP_PROJECT="$PROJECT_ID" "$PY" setup/manage_run_lease.py clear --bucket "$BUCKET" --dataset "$DS" \
    --domain reporting-writer --generation "$generation" --confirm-dataset "$DS" \
    --confirm-no-active-writer >/dev/null

echo "Checking an expired lease fails closed..."
generation=$(make_lease -3600 1)
status=$(invoke expired)
reason=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("reason"))' "$OUT/expired.json")
[[ "$status" == "500" && "$reason" == "lease_expired" ]] || { echo "FAIL: expired lease response status=$status reason=$reason" >&2; exit 1; }
GCP_PROJECT="$PROJECT_ID" "$PY" setup/manage_run_lease.py clear --bucket "$BUCKET" --dataset "$DS" \
    --domain reporting-writer --generation "$generation" --confirm-dataset "$DS" \
    --confirm-no-active-writer >/dev/null

echo "STAGING LEASE TEST: PASS"
