#!/usr/bin/env bash
set -euo pipefail

# Assert the Reporting API raw tables and ledger are consistent, in one dataset.
#
#   bash scripts/verify_reporting.sh youtube_analytics_staging
#   bash scripts/verify_reporting.sh youtube_analytics
#
# Runs the generated structural checks (sql/verification/reporting_structural_checks.sql,
# all 19 tables) and the reconciliation blocks (sql/verification/phase2_reporting_raw.sql),
# substituting only the reporting_* tables into the dataset under test. The Analytics-side
# tables in the reconciliation are always read from PROD_DATASET (default youtube_analytics),
# so staging is compared against the real reference, not against its own seeded copy.
#
# A query that fails, an empty result where a number was expected, an empty ledger, or a
# window with no shared days is a FAIL, never a pass. Read-only. Exit 1 on any failure.

DS="${1:-${BQ_DATASET:-youtube_analytics}}"
PROD_DATASET="${PROD_DATASET:-youtube_analytics}"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project}"
LOCATION="${GCP_REGION:-us-central1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL="$ROOT/sql/verification/phase2_reporting_raw.sql"
STRUCT="$ROOT/sql/verification/reporting_structural_checks.sql"

FAIL=0
q() {  # run a query; a BigQuery error is fatal, never an empty "pass"
    local out
    if ! out=$(bq --project_id="$PROJECT_ID" --location="$LOCATION" query --use_legacy_sql=false --format=csv --quiet "$1" 2>&1); then
        echo "QUERY FAILED: $(echo "$out" | tail -3)" >&2
        exit 2
    fi
    echo "$out"
}
rows() { q "$1" | tail -n +2 | wc -l | tr -d ' '; }
scalar() { local v; v=$(q "$1" | tail -n +2 | head -1); [[ -n "$v" ]] || { echo "EMPTY RESULT for: ${1:0:80}" >&2; exit 2; }; echo "$v"; }
block() {  # tag file: extract one tagged block and retarget the reporting tables to $DS
    local out
    out=$(awk -v t="-- --$1" '$0==t{f=1;next} f&&/^-- -{20,}/{if(started){exit}else{next}} f&&!/^--/{started=1;print}' "$2")
    [[ -n "$out" ]] || { echo "no SQL block tagged $1 in $2" >&2; exit 2; }
    echo "$out" | sed "s/\`youtube_analytics\.reporting_/\`$DS.reporting_/g; s/\`youtube_analytics\.\(daily_\|video_\)/\`$PROD_DATASET.\1/g"
}
check() {  # name expected actual
    if [[ -z "$3" ]]; then echo "FAIL  $1 (empty result)"; FAIL=1
    elif [[ "$2" == "$3" ]]; then echo "PASS  $1 ($3)"
    else echo "FAIL  $1 (expected $2, got $3)"; FAIL=1; fi
}
le() {  # name value threshold  (value must be numeric)
    if [[ ! "$2" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then echo "FAIL  $1 (non-numeric '$2')"; FAIL=1; return; fi
    if awk -v s="$2" -v t="$3" 'BEGIN{exit !(s<=t)}'; then echo "PASS  $1 ($2 <= $3)"; else echo "FAIL  $1 ($2 > $3)"; FAIL=1; fi
}
gt0() { if [[ "$2" =~ ^[0-9]+$ && "$2" -gt 0 ]]; then echo "PASS  $1 ($2)"; else echo "FAIL  $1 ('$2' is not > 0)"; FAIL=1; fi; }

echo "Reporting verification: $PROJECT_ID.$DS (Analytics reference: $PROD_DATASET)"
echo

loaded=$(scalar "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE status='loaded'")
types=$(scalar "SELECT COUNT(DISTINCT report_type) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE status='loaded'")
failed=$(scalar "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE status='failed'")
conflicts=$(scalar "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE status='header_only_conflict'")
superseded=$(scalar "SELECT COUNT(*) FROM \`$PROJECT_ID.$DS.reporting_ingest_ledger\` WHERE status='superseded'")
echo "INFO  ledger: loaded=$loaded types_with_loads=$types failed=$failed header_only_conflict=$conflicts superseded=$superseded"
gt0 "ledger has loaded reports" "$loaded"
gt0 "at least one report type has loads" "$types"

check "grain unique in all 19 raw tables"                0 "$(rows "$(block grain_unique_all "$STRUCT")")"
check "one report_id per partition, all tables"          0 "$(rows "$(block one_report_per_day_all "$STRUCT")")"
check "ledger and tables agree in both directions"       0 "$(rows "$(block ledger_matches_tables_all "$STRUCT")")"
check "ledger has one row per report_id and one loaded per day" 0 "$(rows "$(block ledger_unique "$SQL")")"
check "no failed ledger rows"                            0 "$failed"
check "no header-only conflicts"                         0 "$conflicts"

recon=$(q "$(block reconcile_views_by_video_day "$SQL")")
echo "INFO  reconcile views by video-day:"; echo "$recon" | sed 's/^/      /'
hdr=$(echo "$recon" | head -1); row=$(echo "$recon" | tail -1)
col() {
    local n; n=$(echo "$hdr" | tr ',' '\n' | grep -nx "$1" | cut -d: -f1)
    [[ -n "$n" ]] || { echo "reconciliation output lacks column $1" >&2; exit 2; }
    echo "$row" | cut -d, -f"$n"
}
gt0  "reconciliation window has shared days"               "$(col shared_days)"
check "no day inside the window exists only in Reporting"  0 "$(col days_only_in_reporting)"
check "no day inside the window exists only in Analytics"  0 "$(col days_only_in_analytics)"
le "mean absolute daily views diff within 2% across shared days" "$(col mean_abs_daily_diff_share)" 0.02
echo "INFO  signed net total views diff share (information only): $(col total_views_diff_share)"
le "per-row mismatch share within 3% (about three partial days)" "$(col row_mismatch_share)" 0.03

channel=$(q "$(block reconcile_views_by_channel_day "$SQL")")
echo "INFO  channel-day comparison, most recent shared days:"; echo "$channel" | head -8 | sed 's/^/      /'
check "no one-sided channel-day inside the window" 0 "$(echo "$channel" | tail -n +2 | awk -F, '$6 != "" {n++} END{print n+0}')"
echo "INFO  daily_traffic_sources whole days missing (revealed by the Reporting data):"; q "$(block analytics_table_gaps_revealed "$SQL")" | sed 's/^/      /'
echo "INFO  daily_traffic_sources partial days (videos with views missing):"; q "$(block analytics_partial_days "$SQL")" | sed 's/^/      /'

check "subscribers gained agree within 1 per video-day on shared days" 0 "$(rows "$(block reconcile_subs "$SQL")")"

echo
echo "coverage (last 10 days per report type):"
q "$(block coverage_calendar "$SQL")" | sed 's/^/      /'

exit $FAIL
