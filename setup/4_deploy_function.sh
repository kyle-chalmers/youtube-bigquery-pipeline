#!/usr/bin/env bash
set -euo pipefail

# Deploy the YouTube BigQuery pipeline Cloud Function (2nd gen).
# Requires: APIs enabled (1_enable_apis.sh) and BigQuery tables created (2_create_bigquery.sh).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
# Override FUNCTION_NAME and BQ_DATASET together to deploy the staging copy:
#   FUNCTION_NAME=youtube-bigquery-pipeline-staging BQ_DATASET=youtube_analytics_staging bash setup/4_deploy_function.sh
FUNCTION_NAME="${FUNCTION_NAME:-youtube-bigquery-pipeline}"
BQ_DATASET="${BQ_DATASET:-youtube_analytics}"

# Tuning knobs. These used to exist only as defaults inside main.py, which meant the
# deployed function silently ran whatever the code default was and nobody could tell
# from the deploy what it had been given. Set them explicitly so the deploy is the
# record.
ANALYTICS_LOOKBACK_DAYS="${ANALYTICS_LOOKBACK_DAYS:-5}"
GAP_LOOKBACK_DAYS="${GAP_LOOKBACK_DAYS:-21}"
MAX_GAP_REPAIRS_PER_RUN="${MAX_GAP_REPAIRS_PER_RUN:-5}"
PIPELINE_TZ="${PIPELINE_TZ:-America/Phoenix}"
# Kill switch for the Reporting API ingest step (Phase 2). Off unless a deploy says so.
REPORTING_ENABLED="${REPORTING_ENABLED:-false}"
PROD_FUNCTION="youtube-bigquery-pipeline"
PROD_DATASET="youtube_analytics"

# Required. Previously hardcoded to one channel, which meant anyone deploying this repo
# pointed their function at that channel instead of their own.
: "${YOUTUBE_CHANNEL_ID:?set YOUTUBE_CHANNEL_ID (UC-prefixed) before deploying}"
# The uploads playlist is always the channel id with UC -> UU.
UPLOADS_PLAYLIST_ID="${UPLOADS_PLAYLIST_ID:-UU${YOUTUBE_CHANNEL_ID#UC}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Deploying Cloud Function: $FUNCTION_NAME"
echo "  Project: $PROJECT_ID"
echo "  Region:  $REGION"
echo "  Source:  $REPO_ROOT/cloud_function/"
echo "  Dataset: $BQ_DATASET"
echo "  Lookback: $ANALYTICS_LOOKBACK_DAYS  gap window: $GAP_LOOKBACK_DAYS  max repairs: $MAX_GAP_REPAIRS_PER_RUN  reporting: $REPORTING_ENABLED"
echo ""
# Both directions of the prod/staging cross are refused. The dangerous one is a
# staging-named function left on the default dataset: it would run DELETE-then-load
# against production with rows tagged like the real cron's.
if [[ "$FUNCTION_NAME" == "$PROD_FUNCTION" && "$BQ_DATASET" != "$PROD_DATASET" ]]; then
    echo "Refusing: the production function name with a non-production dataset." >&2
    exit 1
fi
if [[ "$FUNCTION_NAME" != "$PROD_FUNCTION" && "$BQ_DATASET" == "$PROD_DATASET" ]]; then
    echo "Refusing: a non-production function ($FUNCTION_NAME) pointed at the production dataset." >&2
    echo "Set BQ_DATASET together with FUNCTION_NAME, e.g. BQ_DATASET=youtube_analytics_staging." >&2
    exit 1
fi
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"

gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime=python311 \
    --source="$REPO_ROOT/cloud_function/" \
    --entry-point=main \
    --trigger-http \
    --no-allow-unauthenticated \
    --memory=512MB \
    --timeout=540s \
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,BQ_DATASET=$BQ_DATASET,YOUTUBE_CHANNEL_ID=$YOUTUBE_CHANNEL_ID,UPLOADS_PLAYLIST_ID=$UPLOADS_PLAYLIST_ID,PIPELINE_TZ=$PIPELINE_TZ,ANALYTICS_LOOKBACK_DAYS=$ANALYTICS_LOOKBACK_DAYS,GAP_LOOKBACK_DAYS=$GAP_LOOKBACK_DAYS,MAX_GAP_REPAIRS_PER_RUN=$MAX_GAP_REPAIRS_PER_RUN,REPORTING_ENABLED=$REPORTING_ENABLED" \
    --set-secrets="YOUTUBE_API_KEY=youtube-data-api-key:latest" \
    --project="$PROJECT_ID"

echo ""
echo "Deployment complete. Function URL:"
gcloud functions describe "$FUNCTION_NAME" \
    --region="$REGION" \
    --gen2 \
    --format='value(serviceConfig.uri)' \
    --project="$PROJECT_ID"

echo ""
echo "To test manually:"
echo "  FUNCTION_URL=\$(gcloud functions describe $FUNCTION_NAME --region=$REGION --gen2 --format='value(serviceConfig.uri)' --project=$PROJECT_ID)"
echo "  curl -H \"Authorization: bearer \$(gcloud auth print-identity-token)\" \$FUNCTION_URL"
