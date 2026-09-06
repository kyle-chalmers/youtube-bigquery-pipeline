-- traffic_source_type_lookup: Reporting API numeric traffic_source_type codes to names.
-- Grain: code (1 row per code). Timezone: n/a (reference data). Cardinality to any traffic
-- table: 1:n on code. Source: Reporting API dimensions reference
-- (developers.google.com/youtube/reporting/v1/reports/dimensions), matched against this
-- channel's Analytics-API enum names by per-type view totals on 2026-09-05 for the 16 codes
-- with data; the rest follow the Analytics API dimensions reference. Codes 23, 29, 31 and 32
-- have no Analytics API enum at all, so analytics_name is NULL there. Reporting codes are
-- numeric strings; the Analytics API table uses the enum names in `analytics_name`, so the two
-- sources are never joined on type directly, only through this lookup. detail_is_video_id
-- follows the reference; the one observed code-27 row carried a 7-character detail, so treat
-- 27 with care.
CREATE OR REPLACE VIEW `${BQ_DATASET}.traffic_source_type_lookup` AS
SELECT * FROM UNNEST([
  STRUCT('0'  AS code, 'Direct or unknown'              AS name, 'NO_LINK_OTHER'        AS analytics_name, 'direct'      AS surface, FALSE AS detail_is_video_id),
  STRUCT('1',  'YouTube advertising',                    'ADVERTISING',          'ads',         FALSE),
  STRUCT('3',  'Browse features (home, subscriptions)',  'SUBSCRIBER',           'browse',      FALSE),
  STRUCT('4',  'YouTube channel page',                   'YT_CHANNEL',           'channel',     FALSE),
  STRUCT('5',  'YouTube search',                         'YT_SEARCH',            'search',      FALSE),
  STRUCT('7',  'Suggested videos',                       'RELATED_VIDEO',        'suggested',   TRUE),
  STRUCT('8',  'Other YouTube features',                 'YT_OTHER_PAGE',        'other',       FALSE),
  STRUCT('9',  'External',                               'EXT_URL',              'external',    FALSE),
  STRUCT('11', 'Video cards and annotations',            'ANNOTATION',           'cards',       FALSE),
  STRUCT('14', 'Playlists',                              'PLAYLIST',             'playlist',    FALSE),
  STRUCT('17', 'Notifications',                          'NOTIFICATION',         'notification',FALSE),
  STRUCT('18', 'Playlist pages',                         'PLAYLIST',             'playlist',    FALSE),
  STRUCT('19', 'Claimed content programming',            'CAMPAIGN_CARD',        'other',       TRUE),
  STRUCT('20', 'Interactive video end screen',           'END_SCREEN',           'end_screen',  TRUE),
  STRUCT('23', 'Stories',                                CAST(NULL AS STRING),   'other',       FALSE),
  STRUCT('24', 'Shorts feed',                            'SHORTS',               'shorts_feed', FALSE),
  STRUCT('25', 'Product pages',                          'PRODUCT_PAGE',         'other',       FALSE),
  STRUCT('26', 'Hashtag pages',                          'HASHTAGS',             'other',       FALSE),
  STRUCT('27', 'Sound pages',                            'SOUND_PAGE',           'other',       TRUE),
  STRUCT('28', 'Live redirect',                          'LIVE_REDIRECT',        'other',       FALSE),
  STRUCT('29', 'Podcasts',                               CAST(NULL AS STRING),   'other',       FALSE),
  STRUCT('30', 'Remixed video',                          'VIDEO_REMIXES',        'shorts_feed', TRUE),
  STRUCT('31', 'Vertical live feed',                     CAST(NULL AS STRING),   'other',       FALSE),
  STRUCT('32', 'Related video (Shorts content links)',   CAST(NULL AS STRING),   'suggested',   TRUE)
]);
