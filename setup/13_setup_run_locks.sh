#!/usr/bin/env bash
set -euo pipefail

# Provision one environment-specific bucket for pipeline writer leases. The caller must
# name the bucket explicitly so a staging deploy cannot silently share production locks.

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
: "${PIPELINE_LOCK_BUCKET:?set PIPELINE_LOCK_BUCKET explicitly, including the environment name}"
REGION="${GCP_REGION:-us-central1}"

if [[ "$PIPELINE_LOCK_BUCKET" != *"staging"* && "$PIPELINE_LOCK_BUCKET" != *"prod"* ]]; then
    echo "Refusing: PIPELINE_LOCK_BUCKET must contain staging or prod." >&2
    exit 1
fi

if gcloud storage buckets describe "gs://$PIPELINE_LOCK_BUCKET" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "Lock bucket already exists: gs://$PIPELINE_LOCK_BUCKET"
else
    gcloud storage buckets create "gs://$PIPELINE_LOCK_BUCKET" \
        --project="$PROJECT_ID" \
        --location="$REGION" \
        --uniform-bucket-level-access \
        --public-access-prevention
fi

gcloud storage buckets update "gs://$PIPELINE_LOCK_BUCKET" \
    --project="$PROJECT_ID" \
    --uniform-bucket-level-access \
    --pap >/dev/null

if [[ -n "${WRITER_SERVICE_ACCOUNT:-}" ]]; then
    service_account="$WRITER_SERVICE_ACCOUNT"
else
    project_number=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
    service_account="${project_number}-compute@developer.gserviceaccount.com"
fi

gcloud storage buckets add-iam-policy-binding "gs://$PIPELINE_LOCK_BUCKET" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:$service_account" \
    --role="roles/storage.objectUser" >/dev/null

echo "Lock bucket ready: gs://$PIPELINE_LOCK_BUCKET"
echo "Writer principal: $service_account"
