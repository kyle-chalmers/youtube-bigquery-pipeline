#!/usr/bin/env bash
set -euo pipefail

# Create a Cloud Scheduler job to trigger the pipeline daily.
# Requires: Cloud Function deployed (4_deploy_function.sh).
#
# The schedule is stated in America/Phoenix explicitly. An earlier version used
# "50 6 * * *" with no --time-zone, which Cloud Scheduler reads as UTC. That fires
# at the same instant, so it was not a bug on its own, but the script then
# disagreed with the deployed job and read as though the pipeline ran at 6:50am.
# Note this does NOT affect how rows are dated: snapshot_date comes from the
# function (see PIPELINE_TZ in main.py), not from the scheduler.

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="us-central1"
FUNCTION_NAME="youtube-bigquery-pipeline"
JOB_NAME="youtube-daily-snapshot"

# Get the Cloud Function URL
echo "Looking up Cloud Function URL..."
FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" \
    --region="$REGION" \
    --gen2 \
    --format='value(serviceConfig.uri)' \
    --project="$PROJECT_ID")
echo "Function URL: $FUNCTION_URL"

# Get the default compute service account for OIDC authentication
SERVICE_ACCOUNT=$(gcloud iam service-accounts list \
    --filter='displayName:Default compute service account' \
    --format='value(email)' \
    --project="$PROJECT_ID")
echo "Service account: $SERVICE_ACCOUNT"

echo ""
echo "Creating Cloud Scheduler job: $JOB_NAME"
echo "  Schedule: 50 23 * * * America/Phoenix (11:50 PM local, no DST)"
echo "  Retries:  3 with exponential backoff (30s–300s)"
echo ""

gcloud scheduler jobs create http "$JOB_NAME" \
    --location="$REGION" \
    --schedule="50 23 * * *" \
    --time-zone="America/Phoenix" \
    --uri="$FUNCTION_URL" \
    --http-method=POST \
    --oidc-service-account-email="$SERVICE_ACCOUNT" \
    --oidc-token-audience="$FUNCTION_URL" \
    --attempt-deadline=600s \
    --max-retry-attempts=3 \
    --min-backoff=30s \
    --max-backoff=300s \
    --project="$PROJECT_ID"

echo ""
echo "Scheduler job created successfully."
echo ""
echo "To test manually:"
echo "  gcloud scheduler jobs run $JOB_NAME --location=$REGION --project=$PROJECT_ID"
echo ""
echo "To check status:"
echo "  gcloud scheduler jobs describe $JOB_NAME --location=$REGION --project=$PROJECT_ID"
