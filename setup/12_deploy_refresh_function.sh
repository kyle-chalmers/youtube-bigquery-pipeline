#!/usr/bin/env bash
set -euo pipefail

# Deploy the trailing-30-day Analytics refresh as its own Cloud Function (2nd gen).
#
# Same source directory as the daily pipeline and the Reporting ingest, different entry
# point (see setup/9_deploy_reporting_function.sh for the identical reasoning). The build
# env var below points Functions Framework at refresh_main.py instead of main.py.
#
#   FUNCTION_NAME=youtube-analytics-refresh-staging BQ_DATASET=youtube_analytics_staging \
#   REFRESH_TIMEOUT=600s bash setup/12_deploy_refresh_function.sh
#   BQ_DATASET=youtube_analytics REFRESH_TIMEOUT=900s bash setup/12_deploy_refresh_function.sh   # prod
#
# REFRESH_TIMEOUT has NO default and is always required, staging included: run
# setup/refresh_analytics.py once end to end and time it (it prints the elapsed seconds
# and reminds you to use them here) before choosing a number. Cloud Scheduler's own HTTP
# job attempt-deadline ceiling is 30 minutes regardless of this function's own (up to
# 60-minute) timeout — if the measured staging time is uncomfortably close to that,
# that's a signal to shrink REFRESH_TRAILING_DAYS or investigate a range-query
# optimization (see the Phase 4 refresh plan), not to raise this number and hope.
#
# ANALYTICS_LOOKBACK_DAYS has no default either: it must match whatever the daily
# function (youtube-bigquery-pipeline) is actually deployed with, not the code default —
# see analytics_refresh.py's compute_refresh_window docstring for why a mismatch matters.
#
# Env: GCP_PROJECT (or active gcloud project), GCP_REGION (default us-central1),
#      YOUTUBE_CHANNEL_ID is NOT needed (video IDs come from BigQuery, not the Data API —
#      deliberate, spends zero Data API quota), BQ_DATASET (default youtube_analytics),
#      REFRESH_TIMEOUT (required), ANALYTICS_LOOKBACK_DAYS (required),
#      REFRESH_TRAILING_DAYS (default 30), REFRESH_MIN_ROW_RATIO (default 0.5 — see
#      analytics_refresh.py's DEFAULT_MIN_ROW_RATIO docstring; needs Kyle's sign-off
#      before trusting it in prod).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
REGION="${GCP_REGION:-us-central1}"
PROD_FUNCTION="youtube-analytics-refresh"
PROD_DATASET="youtube_analytics"
FUNCTION_NAME="${FUNCTION_NAME:-$PROD_FUNCTION}"
BQ_DATASET="${BQ_DATASET:-$PROD_DATASET}"

: "${REFRESH_TIMEOUT:?set REFRESH_TIMEOUT from a timed setup/refresh_analytics.py run against staging (see the header comment in this script); never guess}"
: "${ANALYTICS_LOOKBACK_DAYS:?set ANALYTICS_LOOKBACK_DAYS to match the value the daily function is deployed with (check setup/4_deploy_function.sh or the live function env), not the code default}"
REFRESH_TRAILING_DAYS="${REFRESH_TRAILING_DAYS:-30}"
REFRESH_MIN_ROW_RATIO="${REFRESH_MIN_ROW_RATIO:-0.5}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Both directions of the prod/staging cross are refused, as in 4_deploy_function.sh and
# 9_deploy_reporting_function.sh.
if [[ "$FUNCTION_NAME" == "$PROD_FUNCTION" && "$BQ_DATASET" != "$PROD_DATASET" ]]; then
    echo "Refusing: the production function name with a non-production dataset." >&2; exit 1
fi
if [[ "$FUNCTION_NAME" != "$PROD_FUNCTION" && "$BQ_DATASET" == "$PROD_DATASET" ]]; then
    echo "Refusing: a non-production function ($FUNCTION_NAME) pointed at the production dataset." >&2; exit 1
fi

echo "Deploying Cloud Function: $FUNCTION_NAME"
echo "  Project: $PROJECT_ID   Region: $REGION"
echo "  Dataset: $BQ_DATASET   Timeout: $REFRESH_TIMEOUT   Lookback: $ANALYTICS_LOOKBACK_DAYS   Trailing days: $REFRESH_TRAILING_DAYS"
echo ""

gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime=python311 \
    --source="$REPO_ROOT/cloud_function/" \
    --entry-point=refresh_main \
    --set-build-env-vars="GOOGLE_FUNCTION_SOURCE=refresh_main.py" \
    --trigger-http \
    --no-allow-unauthenticated \
    --memory=512MB \
    --timeout="$REFRESH_TIMEOUT" \
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,BQ_DATASET=$BQ_DATASET,ANALYTICS_LOOKBACK_DAYS=$ANALYTICS_LOOKBACK_DAYS,REFRESH_TRAILING_DAYS=$REFRESH_TRAILING_DAYS,REFRESH_MIN_ROW_RATIO=$REFRESH_MIN_ROW_RATIO" \
    --project="$PROJECT_ID"
# No YOUTUBE_API_KEY secret binding: this function never touches the Data API. Video IDs
# come from the latest video_metadata snapshot in BigQuery, and the Analytics API uses
# the OAuth refresh token from Secret Manager (loaded by oauth_credentials.py, same as
# the daily function's analytics path and the Reporting ingest) — not the API key.

echo ""
echo "Deployment complete. Function URL:"
gcloud functions describe "$FUNCTION_NAME" --region="$REGION" --gen2 --format='value(serviceConfig.uri)' --project="$PROJECT_ID"
echo ""
echo "Next: point a weekly Cloud Scheduler job at it, scheduled far from the daily"
echo "00:10 Phoenix run, with MAX_RETRY_ATTEMPTS=0 (see setup/5_create_scheduler.sh header):"
echo "  FUNCTION_NAME=$FUNCTION_NAME JOB_NAME=${FUNCTION_NAME}-weekly SCHEDULE=\"0 3 * * 0\" \\"
echo "  ATTEMPT_DEADLINE=$REFRESH_TIMEOUT MAX_RETRY_ATTEMPTS=0 bash setup/5_create_scheduler.sh"
echo ""
echo "To trigger manually:"
echo "  curl -X POST -H \"Authorization: bearer \$(gcloud auth print-identity-token)\" \$(gcloud functions describe $FUNCTION_NAME --region=$REGION --gen2 --format='value(serviceConfig.uri)')"
