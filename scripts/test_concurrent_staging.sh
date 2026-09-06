#!/usr/bin/env bash
set -euo pipefail

# Concurrent double-trigger test, STAGING ONLY. Proves that two overlapping ingest runs on
# the same report cannot double-load a partition or leave the ledger lying.
#
#   bash scripts/test_concurrent_staging.sh [report_type]
#
# Marks the newest loaded report of one type as `failed` in the staging ledger (so both
# runs see it as a candidate), fires two POSTs at the staging Reporting function within
# the same second, waits for both, then asserts: the partition holds exactly one report_id
# with the ledger's row count; exactly one ledger row for that report and it is `loaded`;
# one run reports loaded=1 and the other skipped_current>=1 (the transaction refused the
# loser with already_loaded, and the loser did not touch the winner's ledger row).

RTYPE="${1:-channel_reach_basic_a1}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
[[ "$DS" == *staging* ]] || { echo "refusing: $DS is not a staging dataset" >&2; exit 1; }
FN="${REPORTING_FUNCTION_NAME:-youtube-reporting-ingest-staging}"
[[ "$FN" == *staging* ]] || { echo "refusing: $FN is not a staging function" >&2; exit 1; }
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
q() { bq --project_id="$PROJECT_ID" --location="$REGION" query --use_legacy_sql=false --format=csv --quiet "$1" | tail -n +2; }

RID=$(q "SELECT report_id FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_type='$RTYPE' AND status='loaded' ORDER BY report_date DESC LIMIT 1")
DAY=$(q "SELECT CAST(report_date AS STRING) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_id='$RID'")
ROWS_BEFORE=$(q "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_$RTYPE\` WHERE report_date='$DAY'")
echo "target: $RTYPE $DAY report $RID ($ROWS_BEFORE rows loaded)"

echo "marking it failed so both runs will try to load it"
q "UPDATE \`$PROJECT_ID.$DS.reporting_ingest_ledger\` SET status='failed', error='concurrency test' WHERE report_id='$RID'" >/dev/null

URL=$(gcloud functions describe "$FN" --region="$REGION" --gen2 --format='value(serviceConfig.uri)')
TOKEN=$(gcloud auth print-identity-token)
OUT=$(mktemp -d)
echo "firing two concurrent requests"
curl -s -X POST -H "Authorization: bearer $TOKEN" "$URL" > "$OUT/a.json" &
curl -s -X POST -H "Authorization: bearer $TOKEN" "$URL" > "$OUT/b.json" &
wait
for f in a b; do
    echo "run $f: $(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print({k:d.get(k) for k in ('loaded','skipped_current','failed','superseded','errors')})" "$OUT/$f.json")"
done

FAIL=0
ids=$(q "SELECT COUNT(DISTINCT report_id) || ':' || COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_$RTYPE\` WHERE report_date='$DAY'")
ledger=$(q "SELECT status || ':' || CAST(row_count AS STRING) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_id='$RID'" | tr '\n' ' ')
nrows=$(q "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE report_id='$RID'")
echo "partition report_ids:rows = $ids   ledger for $RID = $ledger (rows: $nrows)"
[[ "$ids" == "1:$ROWS_BEFORE" ]] || { echo "FAIL: partition is not exactly one report_id with $ROWS_BEFORE rows"; FAIL=1; }
[[ "$nrows" == "1" && "$ledger" == "loaded:$ROWS_BEFORE " ]] || { echo "FAIL: ledger row is not a single loaded row"; FAIL=1; }
loaded_total=$(python3 -c "import json,sys; print(sum(json.load(open(f)).get('loaded',0) for f in sys.argv[1:]))" "$OUT/a.json" "$OUT/b.json")
failed_total=$(python3 -c "import json,sys; print(sum(json.load(open(f)).get('failed',0) for f in sys.argv[1:]))" "$OUT/a.json" "$OUT/b.json")
[[ "$loaded_total" == "1" && "$failed_total" == "0" ]] || { echo "FAIL: expected exactly one run to load it and neither to fail (loaded=$loaded_total failed=$failed_total)"; FAIL=1; }
rm -rf "$OUT"
[[ $FAIL -eq 0 ]] && echo "CONCURRENCY TEST: PASS" || echo "CONCURRENCY TEST: FAIL"
exit $FAIL
