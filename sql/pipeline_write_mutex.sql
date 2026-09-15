-- Preseeded transaction fences for independent writer domains. BigQuery detects write
-- conflicts at table granularity, so each domain needs its own physical table. The view
-- keeps one proof surface for operators and read-only verification.
CREATE TABLE IF NOT EXISTS `${BQ_DATASET}.pipeline_write_mutex_analytics` (
  mutex_name STRING NOT NULL,
  touched_at TIMESTAMP NOT NULL
);

MERGE `${BQ_DATASET}.pipeline_write_mutex_analytics` AS target
USING (SELECT 'analytics' AS mutex_name, TIMESTAMP '1970-01-01 00:00:00+00' AS touched_at) AS source
ON target.mutex_name = source.mutex_name
WHEN NOT MATCHED THEN
  INSERT (mutex_name, touched_at) VALUES (source.mutex_name, source.touched_at);

ASSERT (
  SELECT COUNTIF(mutex_name = 'analytics') = 1 AND COUNT(*) = 1
  FROM `${BQ_DATASET}.pipeline_write_mutex_analytics`
) AS 'analytics mutex must exist exactly once';

CREATE TABLE IF NOT EXISTS `${BQ_DATASET}.pipeline_write_mutex_reporting` (
  mutex_name STRING NOT NULL,
  touched_at TIMESTAMP NOT NULL
);

MERGE `${BQ_DATASET}.pipeline_write_mutex_reporting` AS target
USING (SELECT 'reporting' AS mutex_name, TIMESTAMP '1970-01-01 00:00:00+00' AS touched_at) AS source
ON target.mutex_name = source.mutex_name
WHEN NOT MATCHED THEN
  INSERT (mutex_name, touched_at) VALUES (source.mutex_name, source.touched_at);

ASSERT (
  SELECT COUNTIF(mutex_name = 'reporting') = 1 AND COUNT(*) = 1
  FROM `${BQ_DATASET}.pipeline_write_mutex_reporting`
) AS 'reporting mutex must exist exactly once';

CREATE OR REPLACE VIEW `${BQ_DATASET}.pipeline_write_mutex` AS
SELECT mutex_name, touched_at FROM `${BQ_DATASET}.pipeline_write_mutex_analytics`
UNION ALL
SELECT mutex_name, touched_at FROM `${BQ_DATASET}.pipeline_write_mutex_reporting`;

ASSERT (
  SELECT COUNT(*) = 2
  FROM `${BQ_DATASET}.pipeline_write_mutex`
) AS 'pipeline_write_mutex view contains an unexpected domain';
