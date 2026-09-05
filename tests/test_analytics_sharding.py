"""Offline tests for the 200-row cap workaround in youtube_analytics_api.

The behaviour under test exists because the unfiltered video report is a capped
top-N report. See the comment block at the top of youtube_analytics_api.py.
"""

import youtube_analytics_api as ya


def make_api(unfiltered_rows, shard_rows_by_id):
    """An instance whose _query_videos is stubbed, bypassing __init__ and the network."""
    api = ya.YouTubeAnalyticsAPI.__new__(ya.YouTubeAnalyticsAPI)
    calls = {"unfiltered": 0, "shards": []}

    def fake(date_str, video_ids=None):
        if video_ids is None:
            calls["unfiltered"] += 1
            return list(unfiltered_rows)
        calls["shards"].append(list(video_ids))
        return [shard_rows_by_id[v] for v in video_ids if v in shard_rows_by_id]

    api._query_videos = fake
    return api, calls


def row(vid, minutes=1.0):
    return [vid, minutes, 10.0, 50.0, 0, 0, 0]


def test_invariants():
    assert ya.SHARD_SIZE < ya.RESULT_CAP
    assert ya.SHARD_SIZE <= ya.MAX_FILTER_IDS


def test_under_the_cap_is_one_call_no_sharding():
    vids = [f"v{i}" for i in range(50)]
    api, calls = make_api([row(v) for v in vids], {})
    out = api._fetch_video_rows(vids, "2026-08-26")
    assert len(out) == 50
    assert calls["unfiltered"] == 1
    assert calls["shards"] == []


def test_at_the_cap_falls_back_to_sharding():
    many = [f"v{i}" for i in range(450)]
    by_id = {v: row(v) for v in many}
    capped = [row(f"v{i}") for i in range(ya.RESULT_CAP)]
    api, calls = make_api(capped, by_id)
    out = api._fetch_video_rows(many, "2026-08-26")
    expected_shards = -(-len(many) // ya.SHARD_SIZE)
    assert len(out) == 450, "recovers every video, not just the capped 200"
    assert len(calls["shards"]) == expected_shards
    assert all(len(s) <= ya.SHARD_SIZE for s in calls["shards"])
    assert all(len(s) <= ya.MAX_FILTER_IDS for s in calls["shards"])
    assert sorted(i for s in calls["shards"] for i in s) == sorted(many)
    ids = [r[0] for r in out]
    assert len(ids) == len(set(ids)), "merge deduplicates by video id"


def test_overlapping_shards_do_not_duplicate_rows():
    dupe_vids = [f"v{i}" for i in range(10)]
    api, _ = make_api(
        [row(f"v{i}") for i in range(ya.RESULT_CAP)], {v: row(v) for v in dupe_vids}
    )
    out = api._fetch_video_rows(dupe_vids + dupe_vids, "2026-08-26")
    assert len(out) == len({r[0] for r in out})


def test_empty_response_does_not_shard():
    vids = [f"v{i}" for i in range(50)]
    api, calls = make_api([], {})
    assert api._fetch_video_rows(vids, "2026-08-26") == []
    assert calls["shards"] == []
