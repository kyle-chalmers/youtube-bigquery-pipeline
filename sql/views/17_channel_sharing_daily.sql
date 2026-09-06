-- channel_sharing_daily: where shares go, per day per sharing service. A distribution signal
-- for a B2B audience (Slack, Discord, WhatsApp, LinkedIn, email codes).
-- Grain: (report_date, sharing_service). Cardinality: sharing_service aggregated (1:1).
-- videos_shared counts anonymised (NULL) video ids as one bucket.
-- Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Service codes are numeric (Reporting API dimensions reference, read 2026-09-05); the ones
-- named here cover the common social, messaging, email, clipboard and embed codes, and the
-- rest keep their code (`code_N`).
-- Source: reporting_channel_sharing_service_a1 (data from about 2026-09-07).
CREATE OR REPLACE VIEW `${BQ_DATASET}.channel_sharing_daily` AS
SELECT report_date, sharing_service,
       CASE sharing_service WHEN '10' THEN 'facebook' WHEN '31' THEN 'twitter_x' WHEN '46' THEN 'email' WHEN '49' THEN 'whatsapp'
            WHEN '72' THEN 'wechat' WHEN '75' THEN 'telegram' WHEN '97' THEN 'tiktok' WHEN '99' THEN 'snapchat'
            WHEN '102' THEN 'instagram' WHEN '104' THEN 'discord' WHEN '42' THEN 'linkedin' WHEN '53' THEN 'other'
            WHEN '55' THEN 'copy_to_clipboard' WHEN '59' THEN 'embed' WHEN '100' THEN 'android_share_dialog'
            ELSE CONCAT('code_', sharing_service) END AS service_name,
       SUM(shares) AS shares, COUNT(DISTINCT IFNULL(video_id, '<anonymous>')) AS videos_shared
FROM `${BQ_DATASET}.reporting_channel_sharing_service_a1`
GROUP BY 1, 2;
