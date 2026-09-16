#!/usr/bin/env bash
set -euo pipefail

# Stage a replacement, force a BigQuery script failure after DELETE, and prove the
# transaction restored the original staging partition byte-for-byte by row fingerprint.

PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
DS="${BQ_STAGING_DATASET:-youtube_analytics_staging}"
TEST_DATE="${ROLLBACK_TEST_DATE:-1900-01-02}"
[[ "$DS" == *staging* && "$DS" != "youtube_analytics" ]] || { echo "refusing non-staging dataset" >&2; exit 1; }
TABLE="$PROJECT_ID.$DS.daily_video_analytics"
WORK="$PROJECT_ID.$DS._rollback_test_${RANDOM}"

q() { bq --project_id="$PROJECT_ID" --location="$REGION" query --use_legacy_sql=false --format=csv --quiet "$1"; }
cleanup() {
    q "DELETE FROM \`$TABLE\` WHERE activity_date=DATE '$TEST_DATE'; DROP TABLE IF EXISTS \`$WORK\`;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

q "DELETE FROM \`$TABLE\` WHERE activity_date=DATE '$TEST_DATE';
INSERT INTO \`$TABLE\` (activity_date,snapshot_date,video_id,load_source,estimated_minutes_watched,
average_view_duration_seconds,average_view_percentage,impressions,impression_ctr,subscribers_gained,
subscribers_lost,shares,annotation_click_through_rate,card_click_rate)
VALUES (DATE '$TEST_DATE',DATE '$TEST_DATE','__rollback_test__','rollback_original',1.0,60.0,100.0,NULL,NULL,0,0,0,NULL,NULL);
CREATE TABLE \`$WORK\` OPTIONS(expiration_timestamp=TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)) AS
SELECT * REPLACE('rollback_replacement' AS load_source) FROM \`$TABLE\` WHERE activity_date=DATE '$TEST_DATE';" >/dev/null

fingerprint() {
    q "SELECT FORMAT('%d:%d', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t)))) FROM \`$TABLE\` t WHERE activity_date=DATE '$TEST_DATE'" | tail -1
}
before=$(fingerprint)

set +e
output=$(q "BEGIN
  BEGIN TRANSACTION;
  UPDATE \`$PROJECT_ID.$DS.pipeline_write_mutex_analytics\` SET touched_at=CURRENT_TIMESTAMP() WHERE mutex_name='analytics';
  ASSERT @@row_count=1 AS 'mutex singleton required';
  DELETE FROM \`$TABLE\` WHERE activity_date=DATE '$TEST_DATE';
  RAISE USING MESSAGE = 'synthetic crash after delete';
  INSERT INTO \`$TABLE\` SELECT * FROM \`$WORK\`;
  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;" 2>&1)
status=$?
set -e
after=$(fingerprint)

[[ $status -ne 0 && "$output" == *"synthetic crash"* && "$output" == *"after delete"* ]] || { echo "FAIL: forced failure was not observed" >&2; exit 1; }
[[ "$before" == "$after" ]] || { echo "FAIL: partition changed across rollback before=$before after=$after" >&2; exit 1; }
echo "STAGING TRANSACTION ROLLBACK: PASS date=$TEST_DATE fingerprint=$after"
