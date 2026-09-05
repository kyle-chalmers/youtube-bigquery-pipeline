-- Phase 1 verification: the refactor changed nothing.
--
-- Paste ONE tagged block at a time into the BigQuery console and run it (BigQuery allows
-- DECLARE only at the start of a script, so the file cannot run as a whole). Every block
-- is read-only. Each block starts with two DECLAREs naming the datasets; edit those and
-- nothing else. Every block prints its side counts before its diff, so a mismatch can be
-- read as "prod had N, staging had M, joined K" rather than a bare row list.
--
-- Location: the datasets live in us-central1. Because the dataset names sit inside
-- EXECUTE IMMEDIATE strings, BigQuery cannot infer the location and defaults to the US
-- multi-region, which fails with "Dataset ... was not found in location US". In the
-- console set More > Query settings > Data location = us-central1; on the CLI pass
-- `bq query --location=us-central1 --use_legacy_sql=false < block.sql`.
--
-- Note: `bq query --dry_run` validates only the outer script, not the SQL inside
-- EXECUTE IMMEDIATE. Run the block for real to prove it; each costs kilobytes.
--
-- Background: Phase 1 extracted the credential loader and retry helper into shared
-- modules and pointed the backfill at the shared writer. The staging function runs the
-- new code against youtube_analytics_staging; production still runs the old code
-- against youtube_analytics. Same day, same inputs, so the outputs must match. The
-- comparison day is the newest day STAGING wrote. Staging was seeded with a copy of prod,
-- so "newest shared day" would compare the copy with itself and pass trivially. When the
-- HEADER row shows prod_rows=0, prod has not run for that day yet: wait, do not read it
-- as either a pass or a failure.

-- ---------------------------------------------------------------------------
-- --parity_metadata
-- video_metadata for the newest snapshot day staging wrote, every column, full outer join.
-- Expected: the first row is the header (day, prod_rows, staging_rows, joined_rows, diff_rows)
-- with diff_rows = 0 and prod_rows = staging_rows = joined_rows. Any further rows list the
-- differing videos (side = prod_only / staging_only / values_differ).
-- ---------------------------------------------------------------------------
DECLARE prod STRING DEFAULT 'youtube_analytics';
DECLARE staging STRING DEFAULT 'youtube_analytics_staging';

EXECUTE IMMEDIATE FORMAT("""
WITH day AS (SELECT MAX(snapshot_date) AS d FROM `%s.video_metadata`),
p AS (SELECT * FROM `%s.video_metadata` WHERE snapshot_date = (SELECT d FROM day)),
s AS (SELECT * FROM `%s.video_metadata` WHERE snapshot_date = (SELECT d FROM day)),
j AS (
  SELECT COALESCE(p.video_id, s.video_id) AS video_id,
         CASE WHEN p.video_id IS NULL THEN 'staging_only'
              WHEN s.video_id IS NULL THEN 'prod_only'
              WHEN TO_JSON_STRING(p) != TO_JSON_STRING(s) THEN 'values_differ' END AS side,
         TO_JSON_STRING(p) AS prod_row, TO_JSON_STRING(s) AS staging_row
  FROM p FULL OUTER JOIN s USING (video_id)
)
SELECT 'HEADER' AS video_id,
       FORMAT('day=%%t prod_rows=%%d staging_rows=%%d joined_rows=%%d diff_rows=%%d',
              (SELECT d FROM day), (SELECT COUNT(*) FROM p), (SELECT COUNT(*) FROM s),
              (SELECT COUNT(*) FROM j), (SELECT COUNTIF(side IS NOT NULL) FROM j)) AS side,
       NULL AS prod_row, NULL AS staging_row
UNION ALL
SELECT video_id, side, prod_row, staging_row FROM j WHERE side IS NOT NULL
ORDER BY video_id = 'HEADER' DESC, video_id
LIMIT 51
""", staging, prod, staging);

-- ---------------------------------------------------------------------------
-- --parity_stats
-- daily_video_stats for the newest snapshot day staging wrote. The two functions run hours
-- apart inside the same Phoenix day and public counters only rise, so the rule is: key
-- sets identical, and no staging counter exceeds prod. max_view_drift shows how far
-- prod ran ahead (expected small and >= 0).
-- Expected: key_mismatches = 0 and staging_exceeds_prod = 0.
-- ---------------------------------------------------------------------------
DECLARE prod STRING DEFAULT 'youtube_analytics';
DECLARE staging STRING DEFAULT 'youtube_analytics_staging';

EXECUTE IMMEDIATE FORMAT("""
WITH day AS (SELECT MAX(snapshot_date) AS d FROM `%s.daily_video_stats`),
p AS (SELECT * FROM `%s.daily_video_stats` WHERE snapshot_date = (SELECT d FROM day)),
s AS (SELECT * FROM `%s.daily_video_stats` WHERE snapshot_date = (SELECT d FROM day))
SELECT (SELECT d FROM day) AS day,
       (SELECT COUNT(*) FROM p) AS prod_rows,
       (SELECT COUNT(*) FROM s) AS staging_rows,
       (SELECT COUNTIF(p.video_id IS NULL OR s.video_id IS NULL) FROM p FULL OUTER JOIN s USING (video_id)) AS key_mismatches,
       (SELECT COUNTIF(s.view_count > p.view_count OR s.like_count > p.like_count OR s.comment_count > p.comment_count)
          FROM p JOIN s USING (video_id)) AS staging_exceeds_prod,
       (SELECT MAX(p.view_count - s.view_count) FROM p JOIN s USING (video_id)) AS max_view_drift
""", staging, prod, staging);

-- ---------------------------------------------------------------------------
-- --parity_analytics
-- daily_video_analytics for the newest activity date staging holds, every column
-- except snapshot_date and load_source (those legitimately differ). Full outer join.
-- Expected: diff_rows = 0. If prod_rows = 0 the day has not been fetched by prod yet;
-- wait for its run rather than reading 0 as a pass.
-- ---------------------------------------------------------------------------
DECLARE prod STRING DEFAULT 'youtube_analytics';
DECLARE staging STRING DEFAULT 'youtube_analytics_staging';

EXECUTE IMMEDIATE FORMAT("""
WITH day AS (SELECT MAX(activity_date) AS d FROM `%s.daily_video_analytics`),
p AS (SELECT * EXCEPT (snapshot_date, load_source) FROM `%s.daily_video_analytics` WHERE activity_date = (SELECT d FROM day)),
s AS (SELECT * EXCEPT (snapshot_date, load_source) FROM `%s.daily_video_analytics` WHERE activity_date = (SELECT d FROM day)),
j AS (SELECT COALESCE(p.video_id, s.video_id) AS video_id,
             CASE WHEN p.video_id IS NULL THEN 'staging_only' WHEN s.video_id IS NULL THEN 'prod_only'
                  WHEN TO_JSON_STRING(p) != TO_JSON_STRING(s) THEN 'values_differ' END AS side,
             TO_JSON_STRING(p) AS prod_row, TO_JSON_STRING(s) AS staging_row
      FROM p FULL OUTER JOIN s USING (video_id))
SELECT 'HEADER' AS video_id,
       FORMAT('activity_date=%%t prod_rows=%%d staging_rows=%%d joined_rows=%%d diff_rows=%%d',
              (SELECT d FROM day), (SELECT COUNT(*) FROM p), (SELECT COUNT(*) FROM s),
              (SELECT COUNT(*) FROM j), (SELECT COUNTIF(side IS NOT NULL) FROM j)) AS side,
       NULL AS prod_row, NULL AS staging_row
UNION ALL SELECT video_id, side, prod_row, staging_row FROM j WHERE side IS NOT NULL
ORDER BY video_id = 'HEADER' DESC, video_id
LIMIT 51
""", staging, prod, staging);

-- ---------------------------------------------------------------------------
-- --parity_traffic
-- daily_traffic_sources, same rules as analytics, keyed on (video_id, traffic_source_type).
-- Expected: diff_rows = 0.
-- ---------------------------------------------------------------------------
DECLARE prod STRING DEFAULT 'youtube_analytics';
DECLARE staging STRING DEFAULT 'youtube_analytics_staging';

EXECUTE IMMEDIATE FORMAT("""
WITH day AS (SELECT MAX(activity_date) AS d FROM `%s.daily_traffic_sources`),
p AS (SELECT * EXCEPT (snapshot_date, load_source) FROM `%s.daily_traffic_sources` WHERE activity_date = (SELECT d FROM day)),
s AS (SELECT * EXCEPT (snapshot_date, load_source) FROM `%s.daily_traffic_sources` WHERE activity_date = (SELECT d FROM day)),
j AS (SELECT COALESCE(p.video_id, s.video_id) AS video_id, COALESCE(p.traffic_source_type, s.traffic_source_type) AS traffic_source_type,
             CASE WHEN p.video_id IS NULL THEN 'staging_only' WHEN s.video_id IS NULL THEN 'prod_only'
                  WHEN TO_JSON_STRING(p) != TO_JSON_STRING(s) THEN 'values_differ' END AS side,
             TO_JSON_STRING(p) AS prod_row, TO_JSON_STRING(s) AS staging_row
      FROM p FULL OUTER JOIN s USING (video_id, traffic_source_type))
SELECT 'HEADER' AS video_id, '' AS traffic_source_type,
       FORMAT('activity_date=%%t prod_rows=%%d staging_rows=%%d joined_rows=%%d diff_rows=%%d',
              (SELECT d FROM day), (SELECT COUNT(*) FROM p), (SELECT COUNT(*) FROM s),
              (SELECT COUNT(*) FROM j), (SELECT COUNTIF(side IS NOT NULL) FROM j)) AS side,
       NULL AS prod_row, NULL AS staging_row
UNION ALL SELECT video_id, traffic_source_type, side, prod_row, staging_row FROM j WHERE side IS NOT NULL
ORDER BY video_id = 'HEADER' DESC, video_id, traffic_source_type
LIMIT 51
""", staging, prod, staging);

-- ---------------------------------------------------------------------------
-- --partition_fingerprints_diff
-- Every partition of every table, prod vs staging, with row counts and a fingerprint of
-- every column. Partitions present on both sides must be identical; partitions on one
-- side only are listed so you can see exactly what a staging run added.
-- Expected: differing = 0 for every table. staging_only shows the days staging wrote.
-- ---------------------------------------------------------------------------
DECLARE prod STRING DEFAULT 'youtube_analytics';
DECLARE staging STRING DEFAULT 'youtube_analytics_staging';

EXECUTE IMMEDIATE FORMAT("""
WITH fp AS (
  SELECT 'video_metadata' AS t, snapshot_date AS p, COUNT(*) AS n, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))) AS f, 'prod' AS side FROM `%s.video_metadata` x GROUP BY 1,2
  UNION ALL SELECT 'video_metadata', snapshot_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'staging' FROM `%s.video_metadata` x GROUP BY 1,2
  UNION ALL SELECT 'daily_video_stats', snapshot_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'prod' FROM `%s.daily_video_stats` x GROUP BY 1,2
  UNION ALL SELECT 'daily_video_stats', snapshot_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'staging' FROM `%s.daily_video_stats` x GROUP BY 1,2
  UNION ALL SELECT 'daily_video_analytics', activity_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'prod' FROM `%s.daily_video_analytics` x GROUP BY 1,2
  UNION ALL SELECT 'daily_video_analytics', activity_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'staging' FROM `%s.daily_video_analytics` x GROUP BY 1,2
  UNION ALL SELECT 'daily_traffic_sources', activity_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'prod' FROM `%s.daily_traffic_sources` x GROUP BY 1,2
  UNION ALL SELECT 'daily_traffic_sources', activity_date, COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))), 'staging' FROM `%s.daily_traffic_sources` x GROUP BY 1,2
),
pr AS (SELECT t, p, n, f FROM fp WHERE side = 'prod'),
st AS (SELECT t, p, n, f FROM fp WHERE side = 'staging')
SELECT COALESCE(pr.t, st.t) AS table_name,
       COUNTIF(pr.p IS NOT NULL) AS prod_partitions,
       COUNTIF(st.p IS NOT NULL) AS staging_partitions,
       COUNTIF(pr.p IS NOT NULL AND st.p IS NOT NULL) AS shared,
       COUNTIF(pr.p IS NOT NULL AND st.p IS NOT NULL AND (pr.n != st.n OR pr.f != st.f)) AS differing,
       STRING_AGG(IF(pr.p IS NULL, CAST(st.p AS STRING), NULL), ', ' ORDER BY st.p) AS staging_only,
       STRING_AGG(IF(st.p IS NULL, CAST(pr.p AS STRING), NULL), ', ' ORDER BY pr.p) AS prod_only
FROM pr FULL OUTER JOIN st USING (t, p)
GROUP BY 1 ORDER BY 1
""", prod, staging, prod, staging, prod, staging, prod, staging);

-- ---------------------------------------------------------------------------
-- --prod_untouched_summary
-- One row per production table: total rows, partitions, latest partition, whole-table
-- fingerprint. Capture before and after any deploy or staging run and compare.
-- Expected: identical before and after for every table the change did not write.
-- ---------------------------------------------------------------------------
DECLARE ds STRING DEFAULT 'youtube_analytics';

EXECUTE IMMEDIATE FORMAT("""
SELECT 'video_metadata' AS table_name, COUNT(*) AS rows_total, COUNT(DISTINCT snapshot_date) AS partitions,
       MAX(snapshot_date) AS latest, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS fingerprint FROM `%s.video_metadata` t
UNION ALL SELECT 'daily_video_stats', COUNT(*), COUNT(DISTINCT snapshot_date), MAX(snapshot_date), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) FROM `%s.daily_video_stats` t
UNION ALL SELECT 'daily_video_analytics', COUNT(*), COUNT(DISTINCT activity_date), MAX(activity_date), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) FROM `%s.daily_video_analytics` t
UNION ALL SELECT 'daily_traffic_sources', COUNT(*), COUNT(DISTINCT activity_date), MAX(activity_date), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) FROM `%s.daily_traffic_sources` t
ORDER BY 1
""", ds, ds, ds, ds);
