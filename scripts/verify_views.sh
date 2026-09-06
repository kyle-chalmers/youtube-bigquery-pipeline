#!/usr/bin/env bash
set -euo pipefail

# Assert the growth views are honest: no fan-out, ratios recomputed, totals reconcile to the
# raw tables, channel-level subscribers kept, every traffic code named.
#
#   bash scripts/verify_views.sh youtube_analytics_staging
#   bash scripts/verify_views.sh youtube_analytics
#
# Runs the blocks in sql/verification/phase3_views.sql against the dataset. A failed query or
# an empty result is a FAIL, never a pass. Read-only. Exit 1 on any failure.

DS="${1:-${BQ_DATASET:-youtube_analytics}}"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project}"
LOCATION="${GCP_REGION:-us-central1}"
SQL="$(cd "$(dirname "$0")/.." && pwd)/sql/verification/phase3_views.sql"

FAIL=0
# q must ALWAYS be called as `var=$(q ...)` on its own line: inside a command substitution used
# as an argument, its exit 2 would end only the subshell and the empty result could read as a
# pass. stderr goes to a file so a gcloud notice can never become the CSV header.
ERRF=$(mktemp); trap 'rm -f "$ERRF"' EXIT
q() { local out; if ! out=$(bq --project_id="$PROJECT_ID" --location="$LOCATION" query --use_legacy_sql=false --format=csv --quiet --max_rows=100000 "$1" 2>"$ERRF"); then echo "QUERY FAILED: $(tail -3 "$ERRF")" >&2; exit 2; fi; [[ $(echo "$out" | wc -l) -ge 2 ]] || { echo "QUERY RETURNED NO ROWS (header only)" >&2; exit 2; }; echo "$out"; }
num_lt() { awk -v a="$1" -v b="$2" 'BEGIN{ if (a=="" || b=="") print "empty"; else print (a+0 < b+0) ? "yes" : "no" }'; }
num_ge() { awk -v a="$1" -v b="$2" 'BEGIN{ if (a=="" || b=="") print "empty"; else print (a+0 >= b+0) ? "yes" : "no" }'; }
block() { local out; out=$(awk -v t="-- --$1" '$0==t{f=1;next} f&&/^-- -{20,}/{if(started){exit}else{next}} f&&!/^--/{started=1;print}' "$SQL"); [[ -n "$out" ]] || { echo "no block $1" >&2; exit 2; }; echo "$out" | sed "s/\`youtube_analytics\./\`$DS./g"; }
check() { if [[ -z "$3" ]]; then echo "FAIL  $1 (empty result)"; FAIL=1; elif [[ "$2" == "$3" ]]; then echo "PASS  $1 ($3)"; else echo "FAIL  $1 (expected $2, got $3)"; FAIL=1; fi; }
# col is CSV-aware (quoted fields with commas are one field); python3 is present wherever bq is.
col() { python3 -c 'import csv,sys; h=next(csv.reader([sys.argv[1]])); r=next(csv.reader([sys.argv[2]])); n=sys.argv[3]; sys.exit("missing column "+n) if n not in h else print(r[h.index(n)])' "$1" "$2" "$3" || exit 2; }

echo "View verification: $PROJECT_ID.$DS"; echo

grain=$(q "$(block grain_checks)")
echo "INFO  grain per view:"; echo "$grain" | sed 's/^/      /'
check "no view has duplicate grain keys" 0 "$(echo "$grain" | tail -n +2 | awk -F, '{s+=$4} END{print s+0}')"
NVIEWS=$(ls "$(dirname "$SQL")/../views/"*.sql | wc -l | tr -d ' ')
check "every view file has a row in the grain check" "$NVIEWS" "$(echo "$grain" | tail -n +2 | wc -l | tr -d ' ')"
check "the grain check saw data (total rows across views > 0)" yes "$(num_lt 0 "$(echo "$grain" | tail -n +2 | awk -F, '{s+=$2} END{print s+0}')")"

fan=$(q "$(block no_fanout)")
echo "INFO  fan-out check:"; echo "$fan" | sed 's/^/      /'
check "funnel and summary row counts equal their source key counts" 0 "$(echo "$fan" | tail -n +2 | awk -F, '{s+=($4<0?-$4:$4)} END{print s+0}')"

fi_=$(q "$(block funnel_identity)"); h=$(echo "$fi_" | head -1); r=$(echo "$fi_" | tail -1)
echo "INFO  funnel: $r"
check "clicks never exceed impressions on rows with more than 2 impressions" 0 "$(col "$h" "$r" clicks_over_impressions_gt2)"
echo "INFO  rows with clicks > impressions of any size (source ctr > 1 quirk): $(col "$h" "$r" clicks_over_impressions_any)"
ct=$(col "$h" "$r" clicks_total); vt=$(col "$h" "$r" views_total)
check "total clicks below total views (views include non-impression traffic)" yes "$(num_lt "$ct" "$vt")"
check "clicks are exact (impressions * ctr rounding error below 1e-6)" yes "$(num_lt "$(col "$h" "$r" max_click_rounding_error)" 0.000001)"
check "funnel clicks equal ROUND(impressions * ctr) on every row" 0 "$(col "$h" "$r" clicks_formula_mismatch)"

avd=$(q "$(block avd_recompute_check)")
echo "INFO  avd recompute by video type (share of single-segment video-days within 1 s of the source):"; echo "$avd" | sed 's/^/      /'
fl=$(echo "$avd" | awk -F, '$1=="full_length"{print $3}')
check "full-length AVD over views reproduces the source column on >= 90% of single-segment days" yes "$(num_ge "$fl" 0.90)"

rec=$(q "$(block summary_reconciles_to_sources)"); h=$(echo "$rec" | head -1); r=$(echo "$rec" | tail -1)
echo "INFO  summary vs sources: $r"
for c in views_diff gained_diff lost_diff impressions_diff; do check "channel_daily_summary $c is 0" 0 "$(col "$h" "$r" $c)"; done

cl=$(q "$(block channel_level_subscribers_not_dropped)"); h=$(echo "$cl" | head -1); r=$(echo "$cl" | tail -1)
echo "INFO  channel-level subscribers: $r"
a=$(col "$h" "$r" audience_channel_rows); s=$(col "$h" "$r" summary_channel_level); src=$(col "$h" "$r" source_channel_level)
check "channel-level subscriber rows appear in video_audience_growth" yes "$(awk -v a="$a" 'BEGIN{print (a>0)?"yes":"no"}')"
check "channel_daily_summary carries all channel-level subscribers" "$src" "$s"

tc=$(q "$(block traffic_codes_all_named)"); h=$(echo "$tc" | head -1); r=$(echo "$tc" | tail -1)
check "every traffic code in the data is named in the lookup" 0 "$(col "$h" "$r" unnamed_codes)"
check "the traffic code check saw rows" yes "$(num_lt 0 "$(col "$h" "$r" rows_checked)")"

ns=$(q "$(block non_subscriber_split)"); h=$(echo "$ns" | head -1); r=$(echo "$ns" | tail -1)
echo "INFO  non-subscriber split: $r"
check "subscribed_status literals are the ones the views hardcode" "not_subscribed|subscribed" "$(col "$h" "$r" literals)"
check "non-subscriber views are a live number (0 < share < 1)" yes "$( [[ "$(num_lt 0 "$(col "$h" "$r" non_subscriber_views)")" == yes && "$(num_lt "$(col "$h" "$r" non_subscriber_views)" "$(col "$h" "$r" views)")" == yes ]] && echo yes || echo no)"

rw=$(q "$(block rolling_windows)"); h=$(echo "$rw" | head -1); r=$(echo "$rw" | tail -1)
echo "INFO  rolling windows: $r"
for c in rows_7d_wrong rows_28d_wrong windows_over_7_days null_windows; do check "channel_daily_summary $c is 0" 0 "$(col "$h" "$r" $c)"; done

ts=$(q "$(block type_split_reconciles)"); h=$(echo "$ts" | head -1); r=$(echo "$ts" | tail -1)
echo "INFO  type split: $r"
check "shorts + long-form + unclassified views equal views on every day" 0 "$(col "$h" "$r" days_off)"

echo; echo "Studio spot-check rows (compare in Studio Advanced Mode for that Pacific day):"
sc=$(q "$(block studio_spotcheck)"); echo "$sc" | sed 's/^/      /'
check "studio spot-check produced three rows" 3 "$(echo "$sc" | tail -n +2 | wc -l | tr -d ' ')"
exit $FAIL
