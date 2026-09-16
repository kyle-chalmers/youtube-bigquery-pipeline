#!/usr/bin/env bash
set -euo pipefail

# Deploy the complete protected writer set to staging and create paused staging jobs.
# Required inputs are read from the current shell or the existing production function.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project}"
REGION="${GCP_REGION:-us-central1}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
LOCK_BUCKET="${PIPELINE_LOCK_BUCKET:-${PROJECT_ID}-youtube-pipeline-locks-staging}"
REFRESH_TIMEOUT="${REFRESH_TIMEOUT:-1500s}"
ANALYTICS_LOOKBACK_DAYS="${ANALYTICS_LOOKBACK_DAYS:-}"

[[ "$DS" == *staging* && "$DS" != "youtube_analytics" ]] || { echo "refusing non-staging dataset" >&2; exit 1; }
[[ "$LOCK_BUCKET" == *staging* ]] || { echo "refusing non-staging lock bucket" >&2; exit 1; }

if [[ -z "${YOUTUBE_CHANNEL_ID:-}" ]]; then
    YOUTUBE_CHANNEL_ID=$(gcloud functions describe youtube-bigquery-pipeline --gen2 --region="$REGION" \
        --project="$PROJECT_ID" --format='value(serviceConfig.environmentVariables.YOUTUBE_CHANNEL_ID)')
fi
: "${YOUTUBE_CHANNEL_ID:?could not resolve YOUTUBE_CHANNEL_ID from the existing function}"
if [[ -z "$ANALYTICS_LOOKBACK_DAYS" ]]; then
    ANALYTICS_LOOKBACK_DAYS=$(gcloud functions describe youtube-bigquery-pipeline --gen2 --region="$REGION" \
        --project="$PROJECT_ID" --format='value(serviceConfig.environmentVariables.ANALYTICS_LOOKBACK_DAYS)')
fi
: "${ANALYTICS_LOOKBACK_DAYS:?could not resolve the production Analytics lookback}"

GCP_PROJECT="$PROJECT_ID" GCP_REGION="$REGION" PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" \
    bash setup/13_setup_run_locks.sh
GCP_PROJECT="$PROJECT_ID" GCP_REGION="$REGION" BQ_SOURCE_DATASET=youtube_analytics \
    BQ_STAGING_DATASET="$DS" bash setup/8_create_staging.sh

GCP_PROJECT="$PROJECT_ID" GCP_REGION="$REGION" FUNCTION_NAME=youtube-bigquery-pipeline-staging \
    BQ_DATASET="$DS" YOUTUBE_CHANNEL_ID="$YOUTUBE_CHANNEL_ID" PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" \
    ANALYTICS_LOOKBACK_DAYS="$ANALYTICS_LOOKBACK_DAYS" REPORTING_ENABLED=false bash setup/4_deploy_function.sh
GCP_PROJECT="$PROJECT_ID" GCP_REGION="$REGION" FUNCTION_NAME=youtube-reporting-ingest-staging \
    BQ_DATASET="$DS" YOUTUBE_CHANNEL_ID="$YOUTUBE_CHANNEL_ID" PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" \
    REPORTING_ENABLED=true CONFIGURE_ARCHIVE_IAM=false bash setup/9_deploy_reporting_function.sh
GCP_PROJECT="$PROJECT_ID" GCP_REGION="$REGION" FUNCTION_NAME=youtube-analytics-refresh-staging \
    BQ_DATASET="$DS" PIPELINE_LOCK_BUCKET="$LOCK_BUCKET" REFRESH_TIMEOUT="$REFRESH_TIMEOUT" \
    ANALYTICS_LOOKBACK_DAYS="$ANALYTICS_LOOKBACK_DAYS" bash setup/12_deploy_refresh_function.sh

create_and_pause() {
    local function="$1" job="$2" schedule="$3" deadline="$4" retries="$5"
    GCP_PROJECT="$PROJECT_ID" GCP_REGION="$REGION" FUNCTION_NAME="$function" JOB_NAME="$job" \
        SCHEDULE="$schedule" ATTEMPT_DEADLINE="$deadline" MAX_RETRY_ATTEMPTS="$retries" bash setup/5_create_scheduler.sh
    gcloud scheduler jobs pause "$job" --location="$REGION" --project="$PROJECT_ID"
}
create_and_pause youtube-bigquery-pipeline-staging youtube-daily-snapshot-staging "10 0 * * *" 600s 3
create_and_pause youtube-reporting-ingest-staging youtube-reporting-daily-staging "0 8,14 * * *" 1800s 0
create_and_pause youtube-analytics-refresh-staging youtube-analytics-refresh-weekly-staging "0 3 * * 0" "$REFRESH_TIMEOUT" 0

if [[ -n "${ALERT_EMAIL:-}" ]]; then
    GCP_PROJECT="$PROJECT_ID" POLICY_SUFFIX=-staging \
        FUNCTION_NAME=youtube-bigquery-pipeline-staging \
        REPORTING_FUNCTION_NAME=youtube-reporting-ingest-staging \
        REFRESH_FUNCTION_NAME=youtube-analytics-refresh-staging \
        SCHEDULER_JOB_IDS=youtube-daily-snapshot-staging,youtube-reporting-daily-staging,youtube-analytics-refresh-weekly-staging \
        ALERT_EMAIL="$ALERT_EMAIL" bash setup/6_setup_monitoring.sh
else
    echo "ALERT_EMAIL is unset, so staging alert deployment is deferred."
fi

echo "Staging rollout complete. All three staging Scheduler jobs are paused."
