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

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
REGION="${GCP_REGION:-us-central1}"
# Defaults create the daily pipeline job. The Reporting ingest gets its own job at 08:00
# Phoenix, after YouTube has usually generated the previous day's reports:
#   FUNCTION_NAME=youtube-reporting-ingest JOB_NAME=youtube-reporting-daily SCHEDULE="0 8,14 * * *" bash setup/5_create_scheduler.sh
# Twice a day because each report takes about 15 s to load transactionally and a run is
# capped at MAX_REPORTS_PER_RUN; two runs give headroom for 19 jobs plus regenerations.
FUNCTION_NAME="${FUNCTION_NAME:-youtube-bigquery-pipeline}"
JOB_NAME="${JOB_NAME:-youtube-daily-snapshot}"
# 00:10 Phoenix puts the pipeline FIRST in the Data API quota day (the quota resets at Pacific
# midnight, 00:00 Phoenix); at 23:50 it was the last consumer and starved when another tool in
# the project spent the quota (2026-08-14).
SCHEDULE="${SCHEDULE:-10 0 * * *}"

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
echo "  Schedule: $SCHEDULE America/Phoenix (no DST)"
echo "  Retries:  3 with exponential backoff (30s–300s)"
echo ""

# Idempotent: update the job if it exists, create it otherwise.
if gcloud scheduler jobs describe "$JOB_NAME" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    VERB=update
else
    VERB=create
fi
echo "  ($VERB)"
gcloud scheduler jobs "$VERB" http "$JOB_NAME" \
    --location="$REGION" \
    --schedule="$SCHEDULE" \
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
# The scheduler authenticates with an OIDC token for the service account above, which must be
# allowed to invoke the function. Nothing else grants this; the first production ingest run
# on 2026-09-06 failed with "IAM principal lacks run.routes.invoke" because the binding had only
# ever been made by hand for the original function. Idempotent.
echo "Granting invoker on $FUNCTION_NAME to the scheduler's service account..."
gcloud functions add-invoker-policy-binding "$FUNCTION_NAME" \
    --region="$REGION" --project="$PROJECT_ID" \
    --member="serviceAccount:$SERVICE_ACCOUNT" >/dev/null

echo "To test manually:"
echo "  gcloud scheduler jobs run $JOB_NAME --location=$REGION --project=$PROJECT_ID"
echo ""
echo "To check status:"
echo "  gcloud scheduler jobs describe $JOB_NAME --location=$REGION --project=$PROJECT_ID"
