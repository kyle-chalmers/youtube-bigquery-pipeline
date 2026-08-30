#!/usr/bin/env bash
# Regression checks for the 2026-08-29 data audit.
# Every check restates a defect the audit found and asserts it is gone.
# Read-only. Safe to run at any time. Exits non-zero if any check fails.
set -uo pipefail
PROJECT_ID="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
DS="${BQ_DATASET:-youtube_analytics}"
FAILED=0

q() { bq query --project_id="$PROJECT_ID" --use_legacy_sql=false --format=csv --quiet "$1" 2>/dev/null | tail -n +2; }

check() { # name expected actual
    if [[ "$2" == "$3" ]]; then printf "  PASS  %-58s %s\n" "$1" "$3"
    else printf "  FAIL  %-58s expected %s, got %s\n" "$1" "$2" "$3"; FAILED=1; fi
}

echo "Audit regression checks against $PROJECT_ID.$DS"
echo

echo "Finding 1: snapshot_date meant two things"
check "duplicate (activity_date, video_id) in analytics" 0 "$(q "
  SELECT COUNT(*) FROM \`$DS.daily_video_analytics\` a
  JOIN \`$DS.daily_video_analytics\` b ON b.video_id=a.video_id
   AND b.activity_date=a.activity_date AND b.snapshot_date<>a.snapshot_date")"
check "identical metric tuples 3 days apart (the seam signature)" 0 "$(q "
  SELECT COUNT(*) FROM \`$DS.daily_video_analytics\` a
  JOIN \`$DS.daily_video_analytics\` b ON b.video_id=a.video_id
   AND b.activity_date=DATE_ADD(a.activity_date, INTERVAL 3 DAY)
   AND b.estimated_minutes_watched=a.estimated_minutes_watched
   AND b.average_view_percentage=a.average_view_percentage
   AND b.subscribers_gained=a.subscribers_gained AND b.shares=a.shares
  WHERE a.estimated_minutes_watched>0")"
check "videos whose first traffic day precedes publish by >1 day" 0 "$(q "
  WITH m AS (SELECT video_id, DATE(published_at) pub FROM \`$DS.video_metadata\`
             WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM \`$DS.video_metadata\`)),
  f AS (SELECT video_id, MIN(activity_date) first_day FROM \`$DS.daily_traffic_sources\` GROUP BY 1)
  SELECT COUNTIF(DATE_DIFF(f.first_day,m.pub,DAY) > 1) FROM m JOIN f USING (video_id)")"

echo
echo "Finding 2: join silently dropped rows"
check "analytics rows with no metadata row for that video" 39 "$(q "
  SELECT COUNT(*) FROM \`$DS.daily_video_analytics\` a
  LEFT JOIN (SELECT DISTINCT video_id FROM \`$DS.video_metadata\`) m USING (video_id)
  WHERE m.video_id IS NULL")"

echo
echo "Finding 3: gaps in a daily series"
check "missing activity dates, excluding known zero-activity days" 0 "$(q "
  WITH cal AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-10-16', DATE_SUB(CURRENT_DATE(), INTERVAL 6 DAY))) d),
  a AS (SELECT DISTINCT activity_date FROM \`$DS.daily_video_analytics\`)
  SELECT COUNTIF(a.activity_date IS NULL AND cal.d NOT IN ('2025-10-22','2025-10-23'))
  FROM cal LEFT JOIN a ON a.activity_date=cal.d")"
# Bound on the Phoenix date, not UTC. snapshot_date is now stamped in Phoenix local
# time, and today's 23:50 run has not fired yet, so today is legitimately absent.
# Using CURRENT_DATE() here reported a phantom gap: the same UTC-vs-local confusion
# this audit was about, reproduced inside its own regression check.
check "daily_video_stats snapshot gaps since 2026-08-15" 0 "$(q "
  WITH cal AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2026-08-15', DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 1 DAY))) d),
  s AS (SELECT DISTINCT snapshot_date FROM \`$DS.daily_video_stats\`)
  SELECT COUNTIF(s.snapshot_date IS NULL) FROM cal LEFT JOIN s ON s.snapshot_date=cal.d")"

echo
echo "Finding 4: silent truncation at maxResults=200"
check "busiest activity day still under the 200-row cap" "true" "$(q "
  SELECT MAX(n) < 200 FROM (SELECT activity_date, COUNT(*) n FROM \`$DS.daily_video_analytics\` GROUP BY 1)")"

echo
echo "Schema and provenance guarantees"
check "NOT NULL columns on daily_video_analytics" 4 "$(q "
  SELECT COUNT(*) FROM \`$DS.INFORMATION_SCHEMA.COLUMNS\`
  WHERE table_name='daily_video_analytics' AND is_nullable='NO'")"
check "rows missing a load_source tag" 0 "$(q "
  SELECT COUNTIF(load_source IS NULL OR load_source='') FROM \`$DS.daily_video_analytics\`")"
check "analytics partitioned by activity_date" "activity_date" "$(q "
  SELECT column_name FROM \`$DS.INFORMATION_SCHEMA.COLUMNS\`
  WHERE table_name='daily_video_analytics' AND is_partitioning_column='YES'")"
check "traffic partitioned by activity_date" "activity_date" "$(q "
  SELECT column_name FROM \`$DS.INFORMATION_SCHEMA.COLUMNS\`
  WHERE table_name='daily_traffic_sources' AND is_partitioning_column='YES'")"

echo
if [[ $FAILED -eq 0 ]]; then echo "All checks passed."; else echo "FAILURES PRESENT."; fi
exit $FAILED
