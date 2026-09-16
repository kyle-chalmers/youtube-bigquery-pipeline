#!/usr/bin/env bash
set -euo pipefail

# Destructive staging rehearsal for the writer mutex, incident repair, rollback and HTTP
# lease race. This script refuses production names and never targets production tables.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project}"
REGION="${GCP_REGION:-us-central1}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
FN="${REPORTING_FUNCTION_NAME:-youtube-reporting-ingest-staging}"
LOCK_BUCKET="${PIPELINE_LOCK_BUCKET:?set the staging PIPELINE_LOCK_BUCKET}"
PY="${PYTHON:-.venv/bin/python}"
AFFECTED_DATE="${AFFECTED_DATE:-2026-09-07}"
export GCLOUD_ACCESS_TOKEN="${GCLOUD_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"

[[ "$DS" == *staging* && "$DS" != "youtube_analytics" ]] || { echo "refusing non-staging dataset: $DS" >&2; exit 1; }
[[ "$FN" == *staging* ]] || { echo "refusing non-staging function: $FN" >&2; exit 1; }
[[ "$LOCK_BUCKET" == *staging* ]] || { echo "refusing non-staging lock bucket: $LOCK_BUCKET" >&2; exit 1; }

echo "Staging gate: project=$PROJECT_ID dataset=$DS affected_date=$AFFECTED_DATE"
"$PY" setup/generate_reporting_ddl.py --check
"$PY" -m pytest -q

mutex_sql() {
    bq --project_id="$PROJECT_ID" --location="$REGION" query --use_legacy_sql=false --format=csv --quiet "$1"
}
restore_mutex_rows() {
    mutex_sql "DELETE FROM \`$PROJECT_ID.$DS.pipeline_write_mutex_analytics\`; INSERT INTO \`$PROJECT_ID.$DS.pipeline_write_mutex_analytics\` (mutex_name,touched_at) VALUES ('analytics',CURRENT_TIMESTAMP()); DELETE FROM \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\`; INSERT INTO \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\` (mutex_name,touched_at) VALUES ('reporting',CURRENT_TIMESTAMP())" >/dev/null 2>&1 || true
}
trap restore_mutex_rows EXIT
assert_mutex_refuses() {
    local label="$1" output code
    set +e
    output=$(mutex_sql "BEGIN BEGIN TRANSACTION; UPDATE \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\` SET touched_at=CURRENT_TIMESTAMP() WHERE mutex_name='reporting'; ASSERT @@row_count=1 AS 'mutex refusal'; ROLLBACK TRANSACTION; END;" 2>&1)
    code=$?
    set -e
    [[ $code -ne 0 && "$output" == *"mutex"* && "$output" == *"refusal"* ]] || { echo "FAIL: mutex did not refuse $label" >&2; return 1; }
}

echo "Checking missing and duplicate mutex rows fail closed..."
mutex_sql "DELETE FROM \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\` WHERE mutex_name='reporting'" >/dev/null
assert_mutex_refuses "missing row"
mutex_sql "INSERT INTO \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\` (mutex_name,touched_at) VALUES ('reporting',CURRENT_TIMESTAMP()),('reporting',CURRENT_TIMESTAMP())" >/dev/null
assert_mutex_refuses "duplicate rows"
mutex_sql "DELETE FROM \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\` WHERE mutex_name='reporting'; INSERT INTO \`$PROJECT_ID.$DS.pipeline_write_mutex_reporting\` (mutex_name,touched_at) VALUES ('reporting',CURRENT_TIMESTAMP())" >/dev/null

targets=(
    "channel_device_os_a3:$AFFECTED_DATE"
    "channel_traffic_source_a3:$AFFECTED_DATE"
)
target_args=()
for target in "${targets[@]}"; do target_args+=(--target "$target"); done

echo "Seeding one controlled duplicate copy in the two staging partitions and ledger..."
for report_type in channel_device_os_a3 channel_traffic_source_a3; do
    table="reporting_$report_type"
    table_count=$(mutex_sql "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.$table\` WHERE report_date=DATE '$AFFECTED_DATE'" | tail -n +2)
    distinct_count=$(mutex_sql "SELECT COUNT(*) FROM (SELECT DISTINCT * EXCEPT(ingested_at) FROM \`$PROJECT_ID.$DS.$table\` WHERE report_date=DATE '$AFFECTED_DATE')" | tail -n +2)
    ledger_count=$(mutex_sql "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_type='$report_type' AND report_date=DATE '$AFFECTED_DATE'" | tail -n +2)
    [[ "$table_count" -gt 0 && "$table_count" == "$distinct_count" && "$ledger_count" == "1" ]] || {
        echo "FAIL: staging repair seed requires a singular source partition and ledger row for $report_type" >&2
        exit 1
    }
    mutex_sql "INSERT INTO \`$PROJECT_ID.$DS.$table\` SELECT * FROM \`$PROJECT_ID.$DS.$table\` WHERE report_date=DATE '$AFFECTED_DATE'" >/dev/null
    mutex_sql "INSERT INTO \`$PROJECT_ID.$DS.reporting_ingest_ledger\` SELECT * FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_type='$report_type' AND report_date=DATE '$AFFECTED_DATE'" >/dev/null
done

echo "Validating both archived incident partitions without writes..."
GCP_PROJECT="$PROJECT_ID" "$PY" setup/repair_reporting_duplicates.py --dataset "$DS" "${target_args[@]}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
first_prefix="pre_repair_$stamp"
second_prefix="post_rollback_$stamp"
echo "Applying the first staging repair with 30-day snapshots..."
GCP_PROJECT="$PROJECT_ID" PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" "$PY" setup/repair_reporting_duplicates.py \
    --dataset "$DS" "${target_args[@]}" --apply --confirm-dataset "$DS" --snapshot-prefix "$first_prefix"

bash scripts/verify_reporting.sh "$DS"
bash scripts/verify_views.sh "$DS"

echo "Restoring the three staging snapshots to prove rollback..."
for table in reporting_channel_device_os_a3 reporting_channel_traffic_source_a3 reporting_ingest_ledger; do
    mutex_sql "CREATE OR REPLACE TABLE \`$PROJECT_ID.$DS.$table\` CLONE \`$PROJECT_ID.$DS.${table}__${first_prefix}\`" >/dev/null
done

echo "Revalidating restored duplicates and applying the repair a second time..."
GCP_PROJECT="$PROJECT_ID" "$PY" setup/repair_reporting_duplicates.py --dataset "$DS" "${target_args[@]}"
GCP_PROJECT="$PROJECT_ID" PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" "$PY" setup/repair_reporting_duplicates.py \
    --dataset "$DS" "${target_args[@]}" --apply --confirm-dataset "$DS" --snapshot-prefix "$second_prefix"

GCP_PROJECT="$PROJECT_ID" BQ_STAGING_DATASET="$DS" REPORTING_FUNCTION_NAME="$FN" \
    PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" PYTHON="$PY" bash scripts/test_concurrent_staging.sh
GCP_PROJECT="$PROJECT_ID" BQ_STAGING_DATASET="$DS" PYTHON="$PY" \
    bash scripts/test_reporting_transaction_staging.sh
GCP_PROJECT="$PROJECT_ID" BQ_STAGING_DATASET="$DS" PYTHON="$PY" \
    bash scripts/test_cross_domain_mutex_staging.sh
GCP_PROJECT="$PROJECT_ID" BQ_STAGING_DATASET="$DS" REPORTING_FUNCTION_NAME="$FN" \
    PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" PYTHON="$PY" bash scripts/test_leases_staging.sh
GCP_PROJECT="$PROJECT_ID" BQ_STAGING_DATASET="$DS" PYTHON="$PY" \
    bash scripts/test_analytics_concurrent_staging.sh
GCP_PROJECT="$PROJECT_ID" BQ_STAGING_DATASET="$DS" \
    bash scripts/test_transaction_rollback_staging.sh

bash scripts/verify_reporting.sh "$DS"
bash scripts/verify_views.sh "$DS"
"$PY" scripts/verify_staging_alert_scope.py
mutex_rows=$(mutex_sql "SELECT mutex_name, COUNT(*) AS n FROM \`$PROJECT_ID.$DS.pipeline_write_mutex\` GROUP BY mutex_name ORDER BY mutex_name" | tail -n +2)
echo "$mutex_rows"
[[ "$mutex_rows" == $'analytics,1\nreporting,1' ]] || { echo "FAIL: mutex singleton rows are wrong" >&2; exit 1; }

echo "STAGING REMEDIATION VERIFIER: PASS"
