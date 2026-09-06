#!/usr/bin/env bash
set -euo pipefail

# Deploy the YouTube Reporting API ingest as its own Cloud Function (2nd gen).
#
# Same source directory as the daily pipeline, different entry point, so both functions
# share one codebase but fail, time out, and get promoted independently. The Reporting
# function never touches the four original tables. Functions Framework loads main.py unless
# GOOGLE_FUNCTION_SOURCE says otherwise, so the build env var below points it at
# reporting_main.py; that also keeps the two functions' import graphs separate.
#
#   FUNCTION_NAME=youtube-reporting-ingest-staging BQ_DATASET=youtube_analytics_staging REPORTING_ENABLED=true bash setup/9_deploy_reporting_function.sh
#   BQ_DATASET=youtube_analytics REPORTING_ENABLED=false bash setup/9_deploy_reporting_function.sh   # prod, kill switch off
#
# The function's service account needs objectCreator and objectViewer on the archive bucket
# (it writes every downloaded report there, create-if-absent, and never deletes) on top of the
# roles the daily function already has. This script grants those on the bucket only.
#
# Note: REPORTING_ARCHIVE_BUCKET defaults to the production archive for staging deploys too.
# That is deliberate: objects are keyed by report_id and created only if absent, so staging
# and prod ingesting the same report write identical bytes once. Override it to isolate.
#
# Env: GCP_PROJECT (or active gcloud project), GCP_REGION (default us-central1),
#      YOUTUBE_CHANNEL_ID (required), BQ_DATASET (default youtube_analytics),
#      REPORTING_ENABLED (required for the prod function; default true for staging), MAX_REPORTS_PER_RUN (default 30),
#      REPORTING_STALE_DAYS (default 4), REPORTING_ARCHIVE_BUCKET (default <project>-youtube-reporting-raw).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
REGION="${GCP_REGION:-us-central1}"
PROD_FUNCTION="youtube-reporting-ingest"
PROD_DATASET="youtube_analytics"
FUNCTION_NAME="${FUNCTION_NAME:-$PROD_FUNCTION}"
BQ_DATASET="${BQ_DATASET:-$PROD_DATASET}"
# No default for the production function: a redeploy that forgot the flag must not silently
# switch the ingest off (reports expire 60 days after generation). Staging defaults to true.
if [[ "${FUNCTION_NAME:-$PROD_FUNCTION}" == "$PROD_FUNCTION" ]]; then
    : "${REPORTING_ENABLED:?set REPORTING_ENABLED=true or false explicitly when deploying the production function}"
fi
REPORTING_ENABLED="${REPORTING_ENABLED:-true}"
MAX_REPORTS_PER_RUN="${MAX_REPORTS_PER_RUN:-30}"
REPORTING_STALE_DAYS="${REPORTING_STALE_DAYS:-4}"
REPORTING_ARCHIVE_BUCKET="${REPORTING_ARCHIVE_BUCKET:-${PROJECT_ID}-youtube-reporting-raw}"
: "${YOUTUBE_CHANNEL_ID:?set YOUTUBE_CHANNEL_ID (UC-prefixed) before deploying}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Both directions of the prod/staging cross are refused, as in 4_deploy_function.sh.
if [[ "$FUNCTION_NAME" == "$PROD_FUNCTION" && "$BQ_DATASET" != "$PROD_DATASET" ]]; then
    echo "Refusing: the production function name with a non-production dataset." >&2; exit 1
fi
if [[ "$FUNCTION_NAME" != "$PROD_FUNCTION" && "$BQ_DATASET" == "$PROD_DATASET" ]]; then
    echo "Refusing: a non-production function ($FUNCTION_NAME) pointed at the production dataset." >&2; exit 1
fi

echo "Deploying Cloud Function: $FUNCTION_NAME"
echo "  Project: $PROJECT_ID   Region: $REGION"
echo "  Dataset: $BQ_DATASET   Reporting enabled: $REPORTING_ENABLED"
echo "  Archive: gs://$REPORTING_ARCHIVE_BUCKET   Max reports/run: $MAX_REPORTS_PER_RUN   Stale after: ${REPORTING_STALE_DAYS}d"
echo ""

gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime=python311 \
    --source="$REPO_ROOT/cloud_function/" \
    --entry-point=reporting_main \
    --set-build-env-vars="GOOGLE_FUNCTION_SOURCE=reporting_main.py" \
    --trigger-http \
    --no-allow-unauthenticated \
    --memory=512MB \
    --timeout=540s \
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,BQ_DATASET=$BQ_DATASET,YOUTUBE_CHANNEL_ID=$YOUTUBE_CHANNEL_ID,REPORTING_ENABLED=$REPORTING_ENABLED,MAX_REPORTS_PER_RUN=$MAX_REPORTS_PER_RUN,REPORTING_STALE_DAYS=$REPORTING_STALE_DAYS,REPORTING_ARCHIVE_BUCKET=$REPORTING_ARCHIVE_BUCKET" \
    --project="$PROJECT_ID"

SERVICE_ACCOUNT=$(gcloud functions describe "$FUNCTION_NAME" --region="$REGION" --gen2 \
    --format='value(serviceConfig.serviceAccountEmail)' --project="$PROJECT_ID")
echo ""
echo "Granting objectCreator + objectViewer on gs://$REPORTING_ARCHIVE_BUCKET to the function's service account (bucket-level only)..."
# The loader only ever creates objects (if_generation_match=0) and reads metadata. It never
# deletes or overwrites, so it does not get objectAdmin on the only durable copy of the reports.
for role in roles/storage.objectCreator roles/storage.objectViewer; do
    gcloud storage buckets add-iam-policy-binding "gs://$REPORTING_ARCHIVE_BUCKET" \
        --member="serviceAccount:$SERVICE_ACCOUNT" --role="$role" --project="$PROJECT_ID" >/dev/null
done
gcloud storage buckets remove-iam-policy-binding "gs://$REPORTING_ARCHIVE_BUCKET" \
    --member="serviceAccount:$SERVICE_ACCOUNT" --role="roles/storage.objectAdmin" --project="$PROJECT_ID" >/dev/null 2>&1 || true
echo "granted (objectAdmin removed if it was present)"

echo ""
echo "Deployment complete. Function URL:"
gcloud functions describe "$FUNCTION_NAME" --region="$REGION" --gen2 --format='value(serviceConfig.uri)' --project="$PROJECT_ID"
echo ""
echo "To trigger manually:"
echo "  curl -X POST -H \"Authorization: bearer \$(gcloud auth print-identity-token)\" \$(gcloud functions describe $FUNCTION_NAME --region=$REGION --gen2 --format='value(serviceConfig.uri)')"
