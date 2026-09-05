#!/usr/bin/env bash
set -euo pipefail

# Create BigQuery dataset and tables for the YouTube analytics pipeline.
# Requires: setup/1_enable_apis.sh has been run first.
# Env: BQ_DATASET (default youtube_analytics), GCP_REGION (default us-central1).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
DATASET="${BQ_DATASET:-youtube_analytics}"
LOCATION="${GCP_REGION:-us-central1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating BigQuery dataset: $DATASET (location: $LOCATION)"
# Only a genuine pre-existence is tolerated; auth, project, and location failures surface.
if bq --project_id="$PROJECT_ID" show --dataset "$DATASET" >/dev/null 2>&1; then
    echo "Dataset already exists, continuing..."
else
    bq --project_id="$PROJECT_ID" mk \
        --dataset \
        --location="$LOCATION" \
        --description="YouTube channel analytics daily snapshots" \
        "$DATASET"
fi

echo ""
echo "Creating tables from SQL DDL..."
# The DDL carries ${BQ_DATASET} placeholders so one file serves prod and staging.
# Plain sed, so there is no gettext/envsubst prerequisite on a fresh machine.
sed "s/\${BQ_DATASET}/$DATASET/g" "$REPO_ROOT/sql/create_tables.sql" | bq query \
    --project_id="$PROJECT_ID" \
    --use_legacy_sql=false \
    --nouse_cache

echo ""
echo "BigQuery setup complete. Tables in $DATASET:"
bq ls --project_id="$PROJECT_ID" "$DATASET"
