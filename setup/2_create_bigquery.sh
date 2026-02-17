#!/usr/bin/env bash
set -euo pipefail

# Create BigQuery dataset and tables for the YouTube analytics pipeline.
# Requires: setup/1_enable_apis.sh has been run first.

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
DATASET="youtube_analytics"
LOCATION="us-central1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating BigQuery dataset: $DATASET (location: $LOCATION)"
bq --project_id="$PROJECT_ID" mk \
    --dataset \
    --location="$LOCATION" \
    --description="YouTube channel analytics daily snapshots" \
    "$DATASET" 2>/dev/null || echo "Dataset already exists, continuing..."

echo ""
echo "Creating tables from SQL DDL..."
bq query \
    --project_id="$PROJECT_ID" \
    --use_legacy_sql=false \
    --nouse_cache \
    < "$REPO_ROOT/sql/create_tables.sql"

echo ""
echo "BigQuery setup complete. Tables in $DATASET:"
bq ls --project_id="$PROJECT_ID" "$DATASET"
