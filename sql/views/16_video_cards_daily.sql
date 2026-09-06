-- video_cards_daily: are cards worth placing? Impressions, clicks and rates per video per day
-- per card type. This channel does use cards (592 card impressions in the first five weeks).
-- Grain: (report_date, video_id, card_type), summed over card id and the basic dimensions.
-- Cardinality: cards aggregated to the grain FIRST, then LEFT JOIN video_current (n:1).
-- `cards` counts distinct card ids with anonymised (NULL) ids as one bucket. Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Formulas: click_rate = clicks / impressions; teaser_click_rate = teaser clicks / teaser impressions.
-- Card type codes: 0 unknown, 60 link, 61 fundraising, 62 video, 63 playlist, 65 fan funding,
-- 66 merchandise, 68 associated website, 69 channel; anything else keeps its code.
-- Source: reporting_channel_cards_a1 (data from about 2026-09-07).
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_cards_daily` AS
WITH c AS (
  SELECT report_date, video_id, card_type,
         COUNT(DISTINCT IFNULL(card_id, '<anonymous>')) AS cards,
         SUM(card_impressions) AS impressions, SUM(card_clicks) AS clicks,
         SUM(card_teaser_impressions) AS teaser_impressions, SUM(card_teaser_clicks) AS teaser_clicks
  FROM `${BQ_DATASET}.reporting_channel_cards_a1`
  GROUP BY 1, 2, 3
)
SELECT c.report_date, c.video_id, v.title, v.video_type, c.card_type,
       CASE c.card_type WHEN '0' THEN 'unknown' WHEN '60' THEN 'link' WHEN '61' THEN 'fundraising' WHEN '62' THEN 'video' WHEN '63' THEN 'playlist'
            WHEN '65' THEN 'fan_funding' WHEN '66' THEN 'merchandise' WHEN '68' THEN 'associated_website' WHEN '69' THEN 'channel'
            ELSE CONCAT('code_', c.card_type) END AS card_type_name,
       c.cards, c.impressions, c.clicks,
       SAFE_DIVIDE(c.clicks, c.impressions) AS click_rate,
       c.teaser_impressions, c.teaser_clicks,
       SAFE_DIVIDE(c.teaser_clicks, c.teaser_impressions) AS teaser_click_rate
FROM c
LEFT JOIN `${BQ_DATASET}.video_current` v ON v.video_id = c.video_id;
