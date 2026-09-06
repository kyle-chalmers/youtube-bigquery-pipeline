#!/usr/bin/env bash
set -euo pipefail

# Deterministic regeneration test, STAGING ONLY. Proves, on real report files, that:
#   1. an older generation loads when nothing newer is loaded;
#   2. the newer generation then replaces it and the ledger shows superseded -> loaded;
#   3. replaying the older generation afterwards is REFUSED inside the transaction.
#
#   bash scripts/test_regeneration_staging.sh <report_type> <report_date>
#   e.g. bash scripts/test_regeneration_staging.sh channel_traffic_source_a3 2026-08-28
#
# Picks a day that has two generations in the archive, resets that one partition and its
# ledger rows in youtube_analytics_staging, then drives setup/backfill_reporting.py with
# --report-id. Refuses to run against any dataset without "staging" in its name.

RTYPE="${1:?report type}"; DAY="${2:?report date YYYY-MM-DD}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
[[ "$DS" == *staging* ]] || { echo "refusing: $DS is not a staging dataset" >&2; exit 1; }
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
BUCKET="${REPORTING_ARCHIVE_BUCKET:-${PROJECT_ID}-youtube-reporting-raw}"
PY="${PYTHON:-.venv/bin/python}"
# backfill_reporting.py needs the channel id for the in-transaction channel assertion. Read it
# from the staging function's own environment when the shell does not have it.
export YOUTUBE_CHANNEL_ID="${YOUTUBE_CHANNEL_ID:-$(gcloud functions describe youtube-reporting-ingest-staging --region="${GCP_REGION:-us-central1}" --gen2 --format='value(serviceConfig.environmentVariables.YOUTUBE_CHANNEL_ID)' 2>/dev/null)}"
[[ -n "$YOUTUBE_CHANNEL_ID" ]] || { echo "YOUTUBE_CHANNEL_ID is not set and the staging function could not be read" >&2; exit 1; }
q() { bq --project_id="$PROJECT_ID" --location=us-central1 query --use_legacy_sql=false --format=csv --quiet "$1" | tail -n +2; }

echo "generations of $RTYPE $DAY in the archive:"
# bash 3.2 (stock macOS) has no mapfile and no negative array indexes.
GENS=()
while IFS= read -r line; do GENS+=("$line"); done < <(gcloud storage ls -L "gs://$BUCKET/$RTYPE/$DAY/" | awk -F'"' '/"create_time"/{ct=$4} /"report_id"/{print ct, $4}' | sort)
printf '  %s\n' "${GENS[@]}"
[[ ${#GENS[@]} -ge 2 ]] || { echo "need a day with at least two generations in the archive (gcloud storage ls output above)" >&2; exit 1; }
OLD=$(echo "${GENS[0]}" | awk '{print $2}'); NEW=$(echo "${GENS[$((${#GENS[@]}-1))]}" | awk '{print $2}')
echo "old=$OLD new=$NEW"

echo; echo "resetting $DS.reporting_$RTYPE partition $DAY and its ledger rows (staging only)"
q "DELETE FROM \`$PROJECT_ID.$DS.reporting_$RTYPE\` WHERE report_date = '$DAY'" >/dev/null
q "DELETE FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_type = '$RTYPE' AND report_date = '$DAY'" >/dev/null

run() { set +e; $PY setup/backfill_reporting.py --dataset "$DS" --from-gcs --report-type "$RTYPE" --report-id "$1" --load-source regen_test 2>&1 | grep -vE "HTTP 429" | tail -1; set -e; }
state() { q "SELECT report_id, status FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_type='$RTYPE' AND report_date='$DAY' ORDER BY report_create_time" | tr '\n' ' '; }
partition() { q "SELECT ANY_VALUE(report_id) || ':' || COUNT(DISTINCT report_id) || ':' || COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_$RTYPE\` WHERE report_date='$DAY'"; }

FAIL=0
echo; echo "step 1: load OLD generation"; run "$OLD"; echo "  ledger: $(state)  partition(report_id:distinct:rows)=$(partition)"
[[ "$(state)" == *"$OLD,loaded"* && "$(partition)" == "$OLD:1:"* ]] || { echo "FAIL step 1"; FAIL=1; }
echo; echo "step 2: load NEW generation (must supersede)"; run "$NEW"; echo "  ledger: $(state)  partition=$(partition)"
[[ "$(state)" == *"$OLD,superseded"* && "$(state)" == *"$NEW,loaded"* && "$(partition)" == "$NEW:1:"* ]] || { echo "FAIL step 2"; FAIL=1; }
echo; echo "step 3: replay OLD generation through the loader (must be skipped, never downloaded)"; out=$(run "$OLD"); echo "  $out"; echo "  ledger: $(state)  partition=$(partition)"
[[ "$out" == *"'skipped_current': 1"* && "$out" == *"'failed': 0"* && "$(state)" == *"$NEW,loaded"* && "$(state)" == *"$OLD,superseded"* && "$(partition)" == "$NEW:1:"* ]] || { echo "FAIL step 3"; FAIL=1; }

echo; echo "step 3b: replay OLD generation straight into the replacer, bypassing the loader's check (the SQL guard must refuse)"
out=$("$PY" - "$PROJECT_ID" "$DS" "$RTYPE" "$OLD" "$BUCKET" <<'PYEOF' 2>&1
import gzip, sys
sys.path.insert(0, "setup"); sys.path.insert(0, "cloud_function")
import _bootstrap  # noqa: F401
from google.cloud import bigquery, storage
from partition_replacer import ReplaceRefused, StagedTransactionalReplacer
from report_specs import SPECS
from reporting_parser import parse_report
import os
project, ds, rtype, old_id, bucket_name = sys.argv[1:6]
bucket = storage.Client(project=project).bucket(bucket_name)
blob = next(b for b in bucket.list_blobs(prefix=f"{rtype}/") if (b.metadata or {}).get("report_id") == old_id)
m = blob.metadata
rows = parse_report(gzip.decompress(blob.download_as_bytes(raw_download=True)), SPECS[rtype])
rep = StagedTransactionalReplacer(bigquery.Client(project=project), f"{project}.{ds}", os.environ["YOUTUBE_CHANNEL_ID"])
prov = {"report_id": old_id, "job_id": m["job_id"], "report_create_time": m["create_time"], "load_source": "regen_test_direct"}
try:
    rep.replace_partition(SPECS[rtype], rows, prov)
    print("NOT REFUSED")
except ReplaceRefused as e:  # AlreadyLoaded is a ReplaceRefused
    print(f"REFUSED: {e}")
PYEOF
)
echo "  $(echo "$out" | grep -m1 -E "REFUSED|NOT REFUSED")"; echo "  ledger: $(state)  partition=$(partition)"
[[ "$out" == *"REFUSED: "*"equal or newer generation"* && "$(state)" == *"$NEW,loaded"* && "$(state)" == *"$OLD,superseded"* && "$(partition)" == "$NEW:1:"* ]] || { echo "FAIL step 3b"; FAIL=1; }
# Final state is the correct one and needs no patching: NEW loaded, OLD superseded (it really
# was loaded once, in step 1, so superseded is the honest status).
echo; [[ $FAIL -eq 0 ]] && echo "REGENERATION TEST: PASS" || echo "REGENERATION TEST: FAIL"
exit $FAIL
