#!/usr/bin/env bash
set -euo pipefail

# Create a Cloud Monitoring alert that fires when the analytics half of the
# pipeline silently fails (writes 0 rows or throws). The cloud function
# swallows OAuth failures so the Data API path keeps working; this alert
# is what makes those silent failures visible.
#
# Two log strings trigger the alert (both emitted by cloud_function/main.py):
#   1. "Wrote daily_video_analytics — 0 rows"  → analytics call succeeded
#                                                  but returned no data
#   2. "Analytics API failed entirely"          → analytics path crashed
#                                                  (typical cause: invalid_grant)
#
# Requires: ALERT_EMAIL env var (the address to notify).
# Run after the cloud function is deployed (4_deploy_function.sh).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
FUNCTION_NAME="youtube-bigquery-pipeline"
POLICY_NAME="youtube-analytics-failure"
CHANNEL_DISPLAY_NAME="youtube-pipeline-alerts"

if [[ -z "${ALERT_EMAIL:-}" ]]; then
    echo "Error: ALERT_EMAIL env var is required."
    echo "Example: ALERT_EMAIL=you@example.com bash setup/6_setup_monitoring.sh"
    exit 1
fi

echo "Project:       $PROJECT_ID"
echo "Alert email:   $ALERT_EMAIL"
echo "Function:      $FUNCTION_NAME"
echo "Policy:        $POLICY_NAME"
echo ""

# 1. Find or create the email notification channel.
echo "Looking up email notification channel for $ALERT_EMAIL..."
CHANNEL_ID=$(gcloud beta monitoring channels list \
    --project="$PROJECT_ID" \
    --filter="type=email AND labels.email_address=$ALERT_EMAIL" \
    --format="value(name)" | head -1 || true)

if [[ -z "$CHANNEL_ID" ]]; then
    echo "No channel found. Creating new email channel..."
    CHANNEL_ID=$(gcloud beta monitoring channels create \
        --project="$PROJECT_ID" \
        --display-name="$CHANNEL_DISPLAY_NAME" \
        --type=email \
        --channel-labels="email_address=$ALERT_EMAIL" \
        --format="value(name)")
    echo "Created channel: $CHANNEL_ID"
else
    echo "Found existing channel: $CHANNEL_ID"
fi

# 2. Build the alert policy. We use a log-based alert (alpha) which fires
#    on matching log entries directly, no intermediate metric needed.
#    Auto-close after 25h so the next day's run can re-trigger if still broken.
POLICY_JSON=$(mktemp)
cat > "$POLICY_JSON" <<EOF
{
  "displayName": "$POLICY_NAME",
  "documentation": {
    "content": "Analytics half of the YouTube BigQuery pipeline wrote 0 rows. Token expiry is NOT the usual cause; the current token has run for months. Check likely causes in order: (1) the Analytics API had no data yet for the queried activity date, which is what happens when the lookback sits near the edge of availability; (2) one metric in the six-metric query hit a backend issue and zeroed the whole response. Fastest triage: check whether daily_traffic_sources got rows for the same activity date. If it did, credentials are fine. The self-healing gap re-query should recover the day within GAP_LOOKBACK_DAYS.",
    "mimeType": "text/markdown"
  },
  "conditions": [
    {
      "displayName": "Analytics failure log entry",
      "conditionMatchedLog": {
        "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$FUNCTION_NAME\" AND (textPayload:\"Analytics API failed entirely\" OR textPayload:\"Wrote daily_video_analytics — 0 rows\" OR jsonPayload.message:\"Analytics API failed entirely\" OR jsonPayload.message:\"Wrote daily_video_analytics — 0 rows\")"
      }
    }
  ],
  "alertStrategy": {
    "notificationRateLimit": {
      "period": "3600s"
    },
    "autoClose": "90000s"
  },
  "combiner": "OR",
  "enabled": true,
  "notificationChannels": ["$CHANNEL_ID"]
}
EOF

# 3. Create or update the policy.
EXISTING_POLICY=$(gcloud beta monitoring policies list \
    --project="$PROJECT_ID" \
    --filter="displayName=$POLICY_NAME" \
    --format="value(name)" | head -1 || true)

if [[ -z "$EXISTING_POLICY" ]]; then
    echo ""
    echo "Creating alert policy..."
    gcloud beta monitoring policies create \
        --project="$PROJECT_ID" \
        --policy-from-file="$POLICY_JSON"
else
    echo ""
    echo "Updating existing alert policy: $EXISTING_POLICY"
    gcloud beta monitoring policies update "$EXISTING_POLICY" \
        --project="$PROJECT_ID" \
        --policy-from-file="$POLICY_JSON"
fi

rm -f "$POLICY_JSON"

echo ""
echo "Monitoring set up."
echo ""
echo "To test (forces a 0-row run): rotate-out the refresh token, trigger the"
echo "function manually, watch your inbox. Don't forget to rotate it back in."
