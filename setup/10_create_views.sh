#!/usr/bin/env bash
set -euo pipefail

# Create or replace the modeled views in sql/views/ in one dataset, in filename order
# (00_ base relations, 1x_ per-video views, 2x_ channel rollups).
#
#   BQ_DATASET=youtube_analytics_staging bash setup/10_create_views.sh
#   BQ_DATASET=youtube_analytics bash setup/10_create_views.sh
#
# Views hold no data and cannot corrupt any table; CREATE OR REPLACE VIEW is idempotent.
# Each file's header states the view's grain, timezone, cardinality and formulas; the
# grain claims are asserted by scripts/verify_views.sh.
#
# Env: BQ_DATASET (default youtube_analytics), GCP_PROJECT (or active gcloud project),
#      GCP_REGION (default us-central1).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
DATASET="${BQ_DATASET:-youtube_analytics}"
LOCATION="${GCP_REGION:-us-central1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating views in $PROJECT_ID.$DATASET"
for f in "$REPO_ROOT"/sql/views/*.sql; do
    name=$(basename "$f" .sql)
    printf '  %-40s' "$name"
    if sed "s/\${BQ_DATASET}/$DATASET/g" "$f" | bq --project_id="$PROJECT_ID" --location="$LOCATION" query --use_legacy_sql=false --nouse_cache --quiet >/dev/null; then
        echo "ok"
    else
        echo "FAILED"; exit 1
    fi
done
echo "Views in $DATASET:"
bq --project_id="$PROJECT_ID" ls "$DATASET" | awk '$2 == "VIEW" {print "  " $1}'
