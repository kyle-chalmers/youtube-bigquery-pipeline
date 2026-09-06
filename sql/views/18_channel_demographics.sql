-- channel_demographics: who is watching, by age group and gender, per day.
-- Grain: (report_date, age_group, gender). Cardinality: demographics aggregated (1:1).
-- Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Denominator: views_percentage in the source is the share of LOGGED-IN viewers per
-- (video, day, subscribed status, country...) row, not a count. It cannot be summed across
-- rows. This view reports the source rows' average share weighted equally per row as
-- avg_row_share, and the number of source rows; use it as a directional profile only.
-- Sponsorship-deck material more than a growth lever.
-- Source: reporting_channel_demographics_a1 (data from about 2026-09-07).
CREATE OR REPLACE VIEW `${BQ_DATASET}.channel_demographics` AS
SELECT report_date, age_group, gender,
       AVG(views_percentage) AS avg_row_share, COUNT(*) AS source_rows, COUNT(DISTINCT video_id) AS videos
FROM `${BQ_DATASET}.reporting_channel_demographics_a1`
GROUP BY 1, 2, 3;
