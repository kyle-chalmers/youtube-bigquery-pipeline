"""Specs, DDL agreement, and header-driven parsing."""

import subprocess
import sys
from pathlib import Path

import pytest

from report_specs import DIMENSIONS, METRIC_TYPES, SPECS, ReportSpec
from reporting_parser import SchemaDriftError, parse_report

ROOT = Path(__file__).resolve().parent.parent
REACH = SPECS["channel_reach_basic_a1"]
BASIC = SPECS["channel_basic_a3"]


def test_every_spec_column_is_registered_and_typed():
    for spec in SPECS.values():
        assert spec.dimensions[0] == "date"
        assert set(spec.dimensions) <= DIMENSIONS
        assert set(spec.metrics) <= set(METRIC_TYPES)
        for c in spec.columns:
            assert spec.column_type(c) in ("DATE", "STRING", "INT64", "FLOAT64")
        assert spec.table == "reporting_" + spec.report_type


def test_nineteen_report_types_and_no_annotations():
    assert len(SPECS) == 19
    assert not any("annotations" in t for t in SPECS)


def test_committed_ddl_matches_generator():
    r = subprocess.run([sys.executable, str(ROOT / "setup" / "generate_reporting_ddl.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_ddl_only_requires_date_and_channel_in_the_grain():
    ddl = (ROOT / "sql" / "reporting_tables.sql").read_text()
    block = ddl.split("`${BQ_DATASET}.reporting_channel_basic_a3`")[1].split(")\nPARTITION")[0]
    assert "report_date DATE NOT NULL" in block
    assert "channel_id STRING NOT NULL" in block
    assert "video_id STRING," in block and "video_id STRING NOT NULL" not in block


def csv(header, *rows):
    return ("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n").encode()


def test_parse_is_independent_of_column_order():
    a = csv(["date", "channel_id", "video_id", "video_thumbnail_impressions", "video_thumbnail_impressions_ctr"],
            ["20260903", "UC1", "v1", "27", "0.0"])
    b = csv(["video_thumbnail_impressions_ctr", "video_id", "video_thumbnail_impressions", "channel_id", "date"],
            ["0.0", "v1", "27", "UC1", "20260903"])
    assert parse_report(a, REACH) == parse_report(b, REACH) == [
        {"report_date": "2026-09-03", "channel_id": "UC1", "video_id": "v1",
         "video_thumbnail_impressions": 27, "video_thumbnail_impressions_ctr": 0.0}
    ]


def test_parse_types_and_nulls():
    body = csv(["date", "channel_id", "video_id", "video_thumbnail_impressions", "video_thumbnail_impressions_ctr"],
               ["20260903", "UC1", "", "", "12.5"], ["20260903", "UC1", "v2", "3.0", ""])
    rows = parse_report(body, REACH)
    assert rows[0]["video_id"] is None and rows[0]["video_thumbnail_impressions"] is None
    assert rows[0]["video_thumbnail_impressions_ctr"] == 12.5
    assert rows[1]["video_thumbnail_impressions"] == 3 and isinstance(rows[1]["video_thumbnail_impressions"], int)
    assert rows[1]["video_thumbnail_impressions_ctr"] is None


def test_parse_handles_bom_and_no_trailing_newline():
    body = b"\xef\xbb\xbfdate,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n20260903,UC1,v1,1,0.5"
    assert len(parse_report(body, REACH)) == 1


def test_header_only_report_parses_to_no_rows():
    body = csv(["date", "channel_id", "video_id", "video_thumbnail_impressions", "video_thumbnail_impressions_ctr"])
    assert parse_report(body, REACH) == []


@pytest.mark.parametrize("header", [
    ["date", "channel_id", "video_id", "video_thumbnail_impressions"],                                  # missing metric
    ["date", "channel_id", "video_id", "video_thumbnail_impressions", "video_thumbnail_impressions_ctr", "bonus"],  # unknown
    ["date", "channel_id", "video_id", "video_thumbnail_impressions", "video_thumbnail_impressions_ctr", "date"],   # duplicate
])
def test_header_drift_raises(header):
    with pytest.raises(SchemaDriftError):
        parse_report(csv(header, ["20260903"] + ["x"] * (len(header) - 1)), REACH)


def test_bad_values_raise_not_coerce():
    good = ["date", "channel_id", "video_id", "video_thumbnail_impressions", "video_thumbnail_impressions_ctr"]
    with pytest.raises(ValueError):
        parse_report(csv(good, ["2026-09-03", "UC1", "v1", "1", "0"]), REACH)     # ISO date, not YYYYMMDD
    with pytest.raises(ValueError):
        parse_report(csv(good, ["20260903", "UC1", "v1", "abc", "0"]), REACH)     # text in INT64
    with pytest.raises(ValueError):
        parse_report(csv(good, ["20260903", "UC1", "v1", "1.5", "0"]), REACH)     # fractional INT64
    with pytest.raises(SchemaDriftError):
        parse_report(csv(good, ["20260903", "UC1", "v1", "1"]), REACH)            # ragged row


def test_empty_body_is_schema_drift():
    with pytest.raises(SchemaDriftError):
        parse_report(b"", REACH)


def test_real_observed_headers_parse_for_the_three_live_types():
    # Observed 2026-09-05 from live reports; note channel_basic_a3 puts views before engaged_views.
    observed = {
        "channel_reach_basic_a1": "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr",
        "channel_basic_a3": "date,channel_id,video_id,live_or_on_demand,subscribed_status,country_code,views,engaged_views,comments,likes,dislikes,shares,watch_time_minutes,average_view_duration_seconds,average_view_duration_percentage,annotation_impressions,annotation_clickable_impressions,annotation_clicks,annotation_click_through_rate,annotation_closable_impressions,annotation_closes,annotation_close_rate,card_teaser_impressions,card_teaser_clicks,card_teaser_click_rate,card_impressions,card_clicks,card_click_rate,subscribers_gained,subscribers_lost,videos_added_to_playlists,videos_removed_from_playlists,red_views,red_watch_time_minutes",
        "channel_traffic_source_a3": "date,channel_id,video_id,live_or_on_demand,subscribed_status,country_code,traffic_source_type,traffic_source_detail,views,engaged_views,watch_time_minutes,average_view_duration_seconds,average_view_duration_percentage,red_views,red_watch_time_minutes",
    }
    for rtype, header in observed.items():
        assert parse_report((header + "\n").encode(), SPECS[rtype]) == []
