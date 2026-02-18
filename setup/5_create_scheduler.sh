#!/usr/bin/env bash
set -euo pipefail

# Create a Cloud Scheduler job to trigger the pipeline daily.
# Requires: Cloud Function deployed (4_deploy_function.sh).

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
echo "  Schedule: 0 6 * * * (daily at 6:00 AM UTC / midnight CST)"
echo "  Retries:  3 with exponential backoff (30s–300s)"
echo ""

gcloud scheduler jobs create http "$JOB_NAME" \
    --location="$REGION" \
    --schedule="0 6 * * *" \
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
