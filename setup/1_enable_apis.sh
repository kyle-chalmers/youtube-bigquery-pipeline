#!/usr/bin/env bash
set -euo pipefail

# Enable required GCP APIs for the YouTube BigQuery pipeline.
# Run this first before any other setup scripts.

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
echo "Enabling APIs for project: $PROJECT_ID"

gcloud services enable \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    youtubeanalytics.googleapis.com \
    cloudbuild.googleapis.com \
    run.googleapis.com \
    --project="$PROJECT_ID"

echo ""
echo "APIs enabled successfully. Current enabled APIs:"
gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" | sort
