#!/usr/bin/env bash
set -euo pipefail

# Cloud Monitoring alert policies for the pipeline. All are log-based: each fires on an
# exact log string the functions emit, and each notifies the same email channel. If you
# reword a matched string in main.py, reporting_main.py, or reporting_loader.py, change
# it here in the same commit; tests/test_main.py and tests/test_reporting_alerts.py pin
# the strings against this file.
#
# Policies:
#   youtube-analytics-failure   daily pipeline wrote 0 analytics rows or the analytics path crashed
#   youtube-reporting-failure   Reporting ingest crashed, a report failed to load, or a header-only
#                               report tried to supersede a populated day
#   youtube-reporting-stale     the ingest itself found a report type whose newest loaded day is older
#                               than REPORTING_STALE_DAYS (a job stopped generating, or auth broke quietly)
#   youtube-scheduler-failure   Cloud Scheduler reported a non-2xx or a timeout for any job here
#
# Requires: ALERT_EMAIL. Optional: FUNCTION_NAME (default youtube-bigquery-pipeline),
# REPORTING_FUNCTION_NAME (default youtube-reporting-ingest), POLICY_SUFFIX (e.g. "-staging"
# to create a parallel set of policies against the staging functions for a live test).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
FUNCTION_NAME="${FUNCTION_NAME:-youtube-bigquery-pipeline}"
REPORTING_FUNCTION_NAME="${REPORTING_FUNCTION_NAME:-youtube-reporting-ingest}"
POLICY_SUFFIX="${POLICY_SUFFIX:-}"
CHANNEL_DISPLAY_NAME="youtube-pipeline-alerts"

if [[ -z "${ALERT_EMAIL:-}" ]]; then
    echo "Error: ALERT_EMAIL env var is required."
    echo "Example: ALERT_EMAIL=you@example.com bash setup/6_setup_monitoring.sh"
    exit 1
fi

echo "Project:            $PROJECT_ID"
echo "Alert email:        $ALERT_EMAIL"
echo "Pipeline function:  $FUNCTION_NAME"
echo "Reporting function: $REPORTING_FUNCTION_NAME"
echo "Policy suffix:      '${POLICY_SUFFIX}'"
echo ""

# 1. Find or create the email notification channel.
CHANNEL_ID=$(gcloud beta monitoring channels list \
    --project="$PROJECT_ID" \
    --filter="type=\"email\" AND labels.email_address=\"$ALERT_EMAIL\"" \
    --format="value(name)" | head -1)
if [[ -z "$CHANNEL_ID" ]]; then
    echo "Creating email notification channel..."
    CHANNEL_ID=$(gcloud beta monitoring channels create \
        --project="$PROJECT_ID" \
        --display-name="$CHANNEL_DISPLAY_NAME" \
        --type=email \
        --channel-labels="email_address=$ALERT_EMAIL" \
        --format="value(name)")
fi
echo "Notification channel: $CHANNEL_ID"

# Log filter for a function: matches any of the given strings in textPayload or jsonPayload.message.
log_filter() {  # service_name string...
    local svc="$1"; shift
    local parts=()
    for s in "$@"; do
        parts+=("textPayload:\\\"$s\\\"" "jsonPayload.message:\\\"$s\\\"")
    done
    local joined; joined=$(IFS='|'; echo "${parts[*]}")
    joined="${joined//|/ OR }"
    echo "resource.type=\\\"cloud_run_revision\\\" AND resource.labels.service_name=\\\"$svc\\\" AND ($joined)"
}

# Create or update one log-based alert policy. Auto-close after 25h so a still-broken
# pipeline re-alerts the next day; at most one notification per hour.
upsert_policy() {  # name condition_display filter documentation
    local name="$1" cond="$2" filter="$3" doc="$4"
    local json; json=$(mktemp)
    cat > "$json" <<EOF
{
  "displayName": "$name",
  "documentation": {"content": "$doc", "mimeType": "text/markdown"},
  "conditions": [{"displayName": "$cond", "conditionMatchedLog": {"filter": "$filter"}}],
  "alertStrategy": {"notificationRateLimit": {"period": "3600s"}, "autoClose": "90000s"},
  "combiner": "OR",
  "enabled": true,
  "notificationChannels": ["$CHANNEL_ID"]
}
EOF
    local existing
    existing=$(gcloud beta monitoring policies list --project="$PROJECT_ID" \
        --filter="displayName=\"$name\"" --format="value(name)" | head -1)
    if [[ -z "$existing" ]]; then
        echo "Creating alert policy: $name"
        gcloud beta monitoring policies create --project="$PROJECT_ID" --policy-from-file="$json" >/dev/null
    else
        echo "Updating alert policy: $name"
        gcloud beta monitoring policies update "$existing" --project="$PROJECT_ID" --policy-from-file="$json" >/dev/null
    fi
    rm -f "$json"
}

upsert_policy "youtube-analytics-failure${POLICY_SUFFIX}" \
    "Analytics failure log entry" \
    "$(log_filter "$FUNCTION_NAME" "Analytics API failed entirely" "Wrote daily_video_analytics — 0 rows")" \
    "Analytics half of the YouTube BigQuery pipeline wrote 0 rows. Token expiry is NOT the usual cause; the current token has run for months. Check likely causes in order: (1) the Analytics API had no data yet for the queried activity date, which is what happens when the lookback sits near the edge of availability; (2) one metric in the six-metric query hit a backend issue and zeroed the whole response. Fastest triage: check whether daily_traffic_sources got rows for the same activity date. If it did, credentials are fine. The self-healing gap re-query should recover the day within GAP_LOOKBACK_DAYS."

upsert_policy "youtube-reporting-failure${POLICY_SUFFIX}" \
    "Reporting ingest failure log entry" \
    "$(log_filter "$REPORTING_FUNCTION_NAME" "Reporting API failed entirely" "Reporting load error" "Reporting header-only report supersedes populated day" "Reporting API skipped")" \
    "The YouTube Reporting API ingest ($REPORTING_FUNCTION_NAME) hit a failure. Four cases: (0) 'Reporting API skipped' means the function is deployed with REPORTING_ENABLED=false; redeploy with it true, reports expire 60 days after generation. (1) 'failed entirely' means the run crashed before or during listing, usually auth or BigQuery; (2) 'Reporting load error' lists one report that did not load (download, schema drift, or a refused transaction), the ledger row has status failed with the error text, and the next run retries it automatically; (3) 'header-only report supersedes populated day' means YouTube regenerated a day as empty while the table holds rows for it. Nothing was deleted. Inspect the day and, only if the emptying is genuine, apply it with setup/backfill_reporting.py --allow-empty-replace. Query the ledger: SELECT * FROM reporting_ingest_ledger WHERE status IN ('failed','header_only_conflict') ORDER BY ingested_at DESC."

upsert_policy "youtube-reporting-stale${POLICY_SUFFIX}" \
    "Reporting freshness stale log entry" \
    "$(log_filter "$REPORTING_FUNCTION_NAME" "Reporting freshness stale")" \
    "The Reporting ingest ran but its newest loaded report day is older than REPORTING_STALE_DAYS (normal latency is about 2 days). Causes in order of likelihood: YouTube has not generated new reports (check jobs.reports.list via setup/archive_reporting_raw.py --dry-run), a job expired (jobs.list expireTime), or the credential lists zero reports without raising. If jobs show reports the ledger lacks, run setup/backfill_reporting.py against the dataset."

# A run that never happened, or was killed by a timeout, emits no log string at all. Cloud
# Scheduler logs an ERROR when the target returns non-2xx or times out; match that too.
upsert_policy "youtube-scheduler-failure${POLICY_SUFFIX}" \
    "Cloud Scheduler job failure" \
    "resource.type=\\\"cloud_scheduler_job\\\" AND severity>=ERROR" \
    "A Cloud Scheduler job in this project reported an error: the target function returned a non-2xx status or did not answer within the attempt deadline. Check which job (youtube-daily-snapshot or youtube-reporting-daily) and read that function's latest logs. A timeout on the Reporting ingest usually means a large catch-up; it resumes from the ledger on the next run."

echo ""
echo "Monitoring set up. To prove a policy fires, force its condition in staging (POLICY_SUFFIX=-staging"
echo "against the staging functions) and watch the inbox; delete the staging policies afterwards."
