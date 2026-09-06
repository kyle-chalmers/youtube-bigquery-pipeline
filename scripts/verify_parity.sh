#!/usr/bin/env bash
set -euo pipefail

# Prove a code change is behavior-preserving by comparing staging against production.
#
#   bash scripts/verify_parity.sh                 # staging vs prod on the newest day staging wrote (DAY= overrides)
#   bash scripts/verify_parity.sh snapshot        # capture prod partition fingerprints to .planning/parity/
#   bash scripts/verify_parity.sh compare A B     # diff two captures
#
# Exit codes: 0 all comparisons passed; 1 a comparison failed; 2 prod has not run for that day yet
# (run again after both functions have run); 3 a comparison had to be skipped because one
# side has no rows for the day (a skip is not a pass).
#
# The SQL behind each check is in sql/verification/phase1_refactor_parity.sql so you can
# paste it into the console yourself. Read-only against both datasets.
#
# What "identical" means per table. Metadata: every column, keyed on (snapshot_date,
# video_id). Stats: the two functions may run hours apart within the same Phoenix day,
# and public counters only go up, so the key set must match exactly and every staging
# counter must be <= its prod counterpart; the max drift is printed. Analytics and
# traffic: every column except snapshot_date and load_source, keyed on the activity date.

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project}"
PROD="${BQ_DATASET:-youtube_analytics}"
STAGING="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
OUT_DIR=".planning/parity"
mkdir -p "$OUT_DIR"

q() { bq --project_id="$PROJECT_ID" query --use_legacy_sql=false --format=csv --quiet "$1"; }
scalar() { q "$1" | tail -n +2 | head -1; }
count() { q "$1" | tail -n +2 | wc -l | tr -d ' '; }

FAIL=0; SKIPPED=0
check() {  # name expected actual
    if [[ "$2" == "$3" ]]; then echo "PASS  $1 ($3)"; else echo "FAIL  $1 (expected $2, got $3)"; FAIL=1; fi
}
skip() { echo "SKIP  $1"; SKIPPED=1; }

mode="${1:-parity}"
T() { echo "\`$PROJECT_ID.$1.$2\`"; }

if [[ "$mode" == "snapshot" ]]; then
    stamp=$(date +%Y%m%dT%H%M%S)
    file="$OUT_DIR/${PROD}_${stamp}.csv"
    q "SELECT 'video_metadata' t, snapshot_date p, COUNT(*) n, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))) f FROM $(T "$PROD" video_metadata) x GROUP BY 1,2
       UNION ALL SELECT 'daily_video_stats', snapshot_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))) FROM $(T "$PROD" daily_video_stats) x GROUP BY 1,2
       UNION ALL SELECT 'daily_video_analytics', activity_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))) FROM $(T "$PROD" daily_video_analytics) x GROUP BY 1,2
       UNION ALL SELECT 'daily_traffic_sources', activity_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))) FROM $(T "$PROD" daily_traffic_sources) x GROUP BY 1,2
       ORDER BY 1,2" > "$file"
    echo "captured $(($(wc -l < "$file") - 1)) partition fingerprints to $file"
    exit 0
fi

if [[ "$mode" == "compare" ]]; then
    a="$2"; b="$3"
    changed=$(comm -23 <(sort "$a") <(sort "$b") | wc -l | tr -d ' ')
    only_in_b=$(comm -13 <(sort "$a") <(sort "$b") | wc -l | tr -d ' ')
    echo "partitions in $a not identical in $b: $changed"
    comm -23 <(sort "$a") <(sort "$b") | head -20
    echo "new or changed partitions in $b: $only_in_b"
    check "pre-existing partitions unchanged" 0 "$changed"
    exit $FAIL
fi

echo "Parity: $STAGING (new code) vs $PROD (running code), project $PROJECT_ID"
echo

# The day to compare is the newest day the STAGING function wrote (DAY=YYYY-MM-DD
# overrides). Staging was seeded with a copy of prod, so picking "newest shared day" would
# compare the copy against itself and pass trivially. If prod has not run for that day
# yet, exit 2 and say so; that is a wait, not a pass and not a failure.
day="${DAY:-$(scalar "SELECT MAX(snapshot_date) FROM $(T "$STAGING" video_metadata)")}"
prod_has=$(scalar "SELECT COUNT(*) FROM $(T "$PROD" video_metadata) WHERE snapshot_date='$day'")
if [[ "$prod_has" == "0" ]]; then
    echo "prod has no rows for staging's snapshot_date $day yet (prod runs 00:10 America/Phoenix); re-run after it does" >&2
    exit 2
fi
pm=$(scalar "SELECT COUNT(*) FROM $(T "$PROD" video_metadata) WHERE snapshot_date='$day'")
sm=$(scalar "SELECT COUNT(*) FROM $(T "$STAGING" video_metadata) WHERE snapshot_date='$day'")
echo "snapshot_date=$day  prod metadata rows=$pm  staging metadata rows=$sm"

meta_diff=$(count "
WITH p AS (SELECT * FROM $(T "$PROD" video_metadata) WHERE snapshot_date='$day'),
     s AS (SELECT * FROM $(T "$STAGING" video_metadata) WHERE snapshot_date='$day')
SELECT 1 FROM p FULL OUTER JOIN s USING (video_id)
WHERE p.video_id IS NULL OR s.video_id IS NULL OR TO_JSON_STRING(p) != TO_JSON_STRING(s)")
check "video_metadata identical on every column for $day (full outer join)" 0 "$meta_diff"

stats_keys=$(count "
WITH p AS (SELECT video_id FROM $(T "$PROD" daily_video_stats) WHERE snapshot_date='$day'),
     s AS (SELECT video_id FROM $(T "$STAGING" daily_video_stats) WHERE snapshot_date='$day')
SELECT 1 FROM p FULL OUTER JOIN s USING (video_id) WHERE p.video_id IS NULL OR s.video_id IS NULL")
check "daily_video_stats key sets identical for $day" 0 "$stats_keys"
stats_bad=$(count "
SELECT 1 FROM $(T "$PROD" daily_video_stats) p JOIN $(T "$STAGING" daily_video_stats) s USING (snapshot_date, video_id)
WHERE p.snapshot_date='$day' AND (s.view_count > p.view_count OR s.like_count > p.like_count OR s.comment_count > p.comment_count)")
check "daily_video_stats: no staging counter exceeds prod (staging ran earlier in the day)" 0 "$stats_bad"
drift=$(scalar "
SELECT IFNULL(MAX(p.view_count - s.view_count), 0) FROM $(T "$PROD" daily_video_stats) p JOIN $(T "$STAGING" daily_video_stats) s USING (snapshot_date, video_id)
WHERE p.snapshot_date='$day'")
echo "INFO  max view_count drift prod - staging on $day: $drift (intraday growth, expected small and >= 0)"

# Analytics-side comparison keyed on the activity date staging wrote on that snapshot day,
# per table, so a zero-row analytics day does not hide the traffic comparison.
for table in daily_video_analytics daily_traffic_sources; do
    key="video_id"; [[ "$table" == "daily_traffic_sources" ]] && key="video_id, traffic_source_type"
    aday=$(scalar "SELECT MAX(activity_date) FROM $(T "$STAGING" "$table") WHERE snapshot_date='$day'")
    if [[ -z "$aday" ]]; then
        skip "$table: staging wrote no rows on $day"; continue
    fi
    pn=$(scalar "SELECT COUNT(*) FROM $(T "$PROD" "$table") WHERE activity_date='$aday'")
    sn=$(scalar "SELECT COUNT(*) FROM $(T "$STAGING" "$table") WHERE activity_date='$aday'")
    if [[ "$pn" == "0" ]]; then
        skip "$table: prod has no rows for activity_date $aday yet (staging has $sn)"; continue
    fi
    echo "activity_date=$aday  prod $table rows=$pn  staging rows=$sn"
    diff=$(count "
    WITH p AS (SELECT * EXCEPT (snapshot_date, load_source) FROM $(T "$PROD" "$table") WHERE activity_date='$aday'),
         s AS (SELECT * EXCEPT (snapshot_date, load_source) FROM $(T "$STAGING" "$table") WHERE activity_date='$aday')
    SELECT 1 FROM p FULL OUTER JOIN s USING ($key)
    WHERE p.video_id IS NULL OR s.video_id IS NULL OR TO_JSON_STRING(p) != TO_JSON_STRING(s)")
    check "$table identical for activity_date $aday (excluding snapshot_date, load_source; full outer join on $key)" 0 "$diff"
done

if [[ $FAIL -eq 1 ]]; then exit 1; fi
if [[ $SKIPPED -eq 1 ]]; then echo "one or more comparisons skipped; a skip is not a pass"; exit 3; fi
exit 0
