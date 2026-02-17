#!/usr/bin/env bash
set -euo pipefail

# Deploy the YouTube BigQuery pipeline Cloud Function (2nd gen).
# Requires: APIs enabled (1_enable_apis.sh) and BigQuery tables created (2_create_bigquery.sh).

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="us-central1"
FUNCTION_NAME="youtube-bigquery-pipeline"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Deploying Cloud Function: $FUNCTION_NAME"
echo "  Project: $PROJECT_ID"
echo "  Region:  $REGION"
echo "  Source:  $REPO_ROOT/cloud_function/"
echo ""

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
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,BQ_DATASET=youtube_analytics,YOUTUBE_CHANNEL_ID=UCkRi29nXFxNBuPhjseoB6AQ,UPLOADS_PLAYLIST_ID=UUkRi29nXFxNBuPhjseoB6AQ" \
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
