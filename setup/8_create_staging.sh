#!/usr/bin/env bash
set -euo pipefail

# Create the staging dataset and seed it with copies of the four production tables.
#
# Staging is where every change is rehearsed before production sees it. It lives in the
# same project and region as production so the deployed runtime, service account and
# secrets are identical, and the only thing that differs is BQ_DATASET. The seed copies
# give the reconciliation queries real data to compare against.
#
# Direction is enforced, not assumed: the destination must contain "staging" and must
# not be the production dataset, whatever the env vars say. Safe to re-run: the dataset
# create is idempotent and the copies refresh staging from prod with --force.
#
# Env: GCP_PROJECT (or active gcloud project), GCP_REGION (default us-central1),
#      BQ_SOURCE_DATASET (default youtube_analytics), BQ_STAGING_DATASET
#      (default youtube_analytics_staging).

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT_ID:?no GCP project: set GCP_PROJECT or gcloud config set project}"
LOCATION="${GCP_REGION:-us-central1}"
SRC="${BQ_SOURCE_DATASET:-youtube_analytics}"
DST="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
PROD_DATASET="youtube_analytics"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "$SRC" == "$DST" ]]; then
    echo "Refusing: source and staging dataset are the same ($SRC)." >&2
    exit 1
fi
if [[ "$DST" == "$PROD_DATASET" || "$DST" != *staging* ]]; then
    echo "Refusing: destination '$DST' must contain 'staging' and cannot be '$PROD_DATASET'." >&2
    echo "This script copies prod INTO staging with --force; it never writes the other way." >&2
    exit 1
fi

echo "Project:  $PROJECT_ID"
echo "Source:   $SRC"
echo "Staging:  $DST ($LOCATION)"
echo ""

# Only a genuine pre-existence is tolerated; auth, project, and location failures surface.
if bq --project_id="$PROJECT_ID" show --dataset "$DST" >/dev/null 2>&1; then
    echo "Dataset $DST already exists, continuing..."
else
    bq --project_id="$PROJECT_ID" mk \
        --dataset \
        --location="$LOCATION" \
        --description="Staging copy of $SRC for rehearsing pipeline changes. Never read by production." \
        "$DST"
fi

echo ""
echo "Creating tables from DDL (CREATE TABLE IF NOT EXISTS)..."
GCP_PROJECT="$PROJECT_ID" BQ_DATASET="$DST" GCP_REGION="$LOCATION" bash "$SCRIPT_DIR/2_create_bigquery.sh" >/dev/null

echo ""
for table in video_metadata daily_video_stats daily_video_analytics daily_traffic_sources; do
    echo "Copying $SRC.$table -> $DST.$table"
    bq --project_id="$PROJECT_ID" cp --force --quiet "$SRC.$table" "$DST.$table"
done

echo ""
echo "Row counts, source vs staging (must match):"
FAIL=0
for table in video_metadata daily_video_stats daily_video_analytics daily_traffic_sources; do
    line=$(bq --project_id="$PROJECT_ID" query --use_legacy_sql=false --format=csv --quiet \
        "SELECT (SELECT COUNT(*) FROM \`$PROJECT_ID.$SRC.$table\`) AS src, (SELECT COUNT(*) FROM \`$PROJECT_ID.$DST.$table\`) AS staging" | tail -1)
    src="${line%%,*}"; dst="${line##*,}"
    if [[ "$src" == "$dst" ]]; then
        echo "  OK    $table  $src = $dst"
    else
        echo "  FAIL  $table  src=$src staging=$dst" >&2
        FAIL=1
    fi
done
exit $FAIL
