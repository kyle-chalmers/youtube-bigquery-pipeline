#!/usr/bin/env bash
# Check that the YouTube BigQuery pipeline has been running successfully
# for the past several days. Run after: gcloud auth login (and optionally
# gcloud auth application-default login).
# Usage: ./scripts/check_recent_runs.sh

set -e
PROJECT="primeval-node-478707-e9"
DATASET="youtube_analytics"
DAYS="${1:-5}"

echo "=== Pipeline health check (last ${DAYS} days) ==="
echo ""

echo "1. Snapshot dates in video_metadata (Data API):"
bq query --use_legacy_sql=false --format=pretty "
SELECT snapshot_date, COUNT(*) AS video_count
FROM \`${PROJECT}.${DATASET}.video_metadata\`
WHERE snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL ${DAYS} DAY)
GROUP BY snapshot_date
ORDER BY snapshot_date DESC
"

echo ""
echo "2. Snapshot dates in daily_video_stats (Data API):"
bq query --use_legacy_sql=false --format=pretty "
SELECT snapshot_date, COUNT(*) AS video_count
FROM \`${PROJECT}.${DATASET}.daily_video_stats\`
WHERE snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL ${DAYS} DAY)
GROUP BY snapshot_date
ORDER BY snapshot_date DESC
"

echo ""
echo "3. Latest snapshot dates across all tables:"
bq query --use_legacy_sql=false --format=pretty "
SELECT 'video_metadata' AS tbl, MAX(snapshot_date) AS latest FROM \`${PROJECT}.${DATASET}.video_metadata\`
UNION ALL
SELECT 'daily_video_stats', MAX(snapshot_date) FROM \`${PROJECT}.${DATASET}.daily_video_stats\`
UNION ALL
SELECT 'daily_video_analytics', MAX(snapshot_date) FROM \`${PROJECT}.${DATASET}.daily_video_analytics\`
UNION ALL
SELECT 'daily_traffic_sources', MAX(snapshot_date) FROM \`${PROJECT}.${DATASET}.daily_traffic_sources\`
ORDER BY latest DESC
"

echo ""
echo "4. Recent Cloud Function executions (last 10):"
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="youtube-bigquery-pipeline" AND (textPayload=~"Pipeline (started|complete|failed)" OR jsonPayload.message=~"Pipeline (started|complete|failed)")' \
  --limit=10 \
  --format="table(timestamp, textPayload, jsonPayload.message)" \
  --freshness=5d \
  --project="${PROJECT}" 2>/dev/null || echo "Run: gcloud auth login (and application-default login) if this fails."

echo ""
echo "Done. Expect one snapshot_date per calendar day for video_metadata/daily_video_stats (scheduler runs ~11:50 PM Phoenix)."
