-- video_current: one row per video, from the newest video_metadata snapshot.
-- Grain: video_id (1 row per video). Cardinality to any per-video-day table: 1:n.
-- Timezone: n/a (metadata). Source: video_metadata (Data API), latest snapshot_date only.
-- This is the ONLY metadata relation other views may join. video_metadata itself is one
-- row per video per day and joining it directly multiplies rows by the number of days.
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_current` AS
SELECT video_id, title, published_at, duration_seconds, duration_formatted, video_type,
       tags, category_id, thumbnail_url, snapshot_date AS as_of_snapshot_date
FROM `${BQ_DATASET}.video_metadata`
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `${BQ_DATASET}.video_metadata`);
