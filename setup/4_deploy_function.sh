#!/usr/bin/env bash
set -euo pipefail

# Deploy the YouTube BigQuery pipeline Cloud Function (2nd gen).
# Requires: APIs enabled (1_enable_apis.sh) and BigQuery tables created (2_create_bigquery.sh).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
# Override FUNCTION_NAME and BQ_DATASET together to deploy the staging copy:
#   FUNCTION_NAME=youtube-bigquery-pipeline-staging BQ_DATASET=youtube_analytics_staging bash setup/4_deploy_function.sh
# Required env: YOUTUBE_CHANNEL_ID and the environment-specific PIPELINE_LOCK_BUCKET.
# RUN_LEASE_TTL_SECONDS defaults to 2100 and must pass the deploy safety margin.
FUNCTION_NAME="${FUNCTION_NAME:-youtube-bigquery-pipeline}"
BQ_DATASET="${BQ_DATASET:-youtube_analytics}"

# Tuning knobs. These used to exist only as defaults inside main.py, which meant the
# deployed function silently ran whatever the code default was and nobody could tell
# from the deploy what it had been given. Set them explicitly so the deploy is the
# record.
ANALYTICS_LOOKBACK_DAYS="${ANALYTICS_LOOKBACK_DAYS:-6}"
GAP_LOOKBACK_DAYS="${GAP_LOOKBACK_DAYS:-21}"
MAX_GAP_REPAIRS_PER_RUN="${MAX_GAP_REPAIRS_PER_RUN:-5}"
PIPELINE_TZ="${PIPELINE_TZ:-America/Phoenix}"
# Kill switch for the Reporting API ingest step (Phase 2). Off unless a deploy says so.
REPORTING_ENABLED="${REPORTING_ENABLED:-false}"
RUN_LEASE_TTL_SECONDS="${RUN_LEASE_TTL_SECONDS:-2100}"
: "${PIPELINE_LOCK_BUCKET:?set PIPELINE_LOCK_BUCKET to the environment-specific writer-lock bucket}"
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
# staging-named function left on the default dataset: it would transactionally replace
# production partitions with rows tagged like the real cron's.
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
source "$SCRIPT_DIR/deploy_safety.sh"
validate_writer_deploy "$FUNCTION_NAME" "$BQ_DATASET" "$PIPELINE_LOCK_BUCKET" \
    "$PROJECT_ID" 540 "$RUN_LEASE_TTL_SECONDS"

gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime=python311 \
    --source="$REPO_ROOT/cloud_function/" \
    --entry-point=main \
    --trigger-http \
    --no-allow-unauthenticated \
    --memory=512MB \
    --cpu=1 \
    --timeout=540s \
    --max-instances=1 \
    --concurrency=2 \
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,BQ_DATASET=$BQ_DATASET,YOUTUBE_CHANNEL_ID=$YOUTUBE_CHANNEL_ID,UPLOADS_PLAYLIST_ID=$UPLOADS_PLAYLIST_ID,PIPELINE_TZ=$PIPELINE_TZ,ANALYTICS_LOOKBACK_DAYS=$ANALYTICS_LOOKBACK_DAYS,GAP_LOOKBACK_DAYS=$GAP_LOOKBACK_DAYS,MAX_GAP_REPAIRS_PER_RUN=$MAX_GAP_REPAIRS_PER_RUN,REPORTING_ENABLED=$REPORTING_ENABLED,PIPELINE_LOCK_BUCKET=$PIPELINE_LOCK_BUCKET,RUN_LEASE_TTL_SECONDS=$RUN_LEASE_TTL_SECONDS" \
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
