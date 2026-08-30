#!/usr/bin/env python3
"""Offline tests for the 200-row cap workaround in youtube_analytics_api.

No credentials and no network. Run: python3 tests/test_analytics_sharding.py

The behaviour under test exists because the unfiltered video report is a capped
top-N report. See the comment block at the top of youtube_analytics_api.py.
"""
import sys, types, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud_function"))

# stub the two google deps so the module imports without credentials
sys.modules.setdefault("google.cloud.secretmanager", types.SimpleNamespace(
    SecretManagerServiceClient=lambda *a, **k: None))
sys.modules.setdefault("googleapiclient.discovery", types.SimpleNamespace(
    build=lambda *a, **k: None))
sys.modules.setdefault("googleapiclient.errors", types.SimpleNamespace(HttpError=Exception))
sys.modules.setdefault("google.oauth2.credentials", types.SimpleNamespace(
    Credentials=lambda *a, **k: None))

import youtube_analytics_api as ya

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def make_api(unfiltered_rows, shard_rows_by_id):
    """An instance whose _query_videos is stubbed, bypassing __init__/network."""
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


print("Invariants")
check("SHARD_SIZE stays under the row cap", ya.SHARD_SIZE < ya.RESULT_CAP,
      f"({ya.SHARD_SIZE} < {ya.RESULT_CAP})")
check("SHARD_SIZE stays within the filter id limit", ya.SHARD_SIZE <= ya.MAX_FILTER_IDS,
      f"({ya.SHARD_SIZE} <= {ya.MAX_FILTER_IDS})")

print("\nUnder the cap: one call, no sharding")
vids = [f"v{i}" for i in range(50)]
api, calls = make_api([row(v) for v in vids], {})
out = api._fetch_video_rows(vids, "2026-08-26")
check("returns every row", len(out) == 50, f"({len(out)})")
check("makes exactly one unfiltered call", calls["unfiltered"] == 1)
check("does not shard", calls["shards"] == [])

print("\nAt the cap: falls back to sharding")
many = [f"v{i}" for i in range(450)]
by_id = {v: row(v) for v in many}
capped = [row(f"v{i}") for i in range(ya.RESULT_CAP)]      # exactly at the cap
api, calls = make_api(capped, by_id)
out = api._fetch_video_rows(many, "2026-08-26")
expected_shards = -(-len(many) // ya.SHARD_SIZE)
check("recovers every video, not just the capped 200", len(out) == 450, f"({len(out)})")
check("shards the full id list", len(calls["shards"]) == expected_shards,
      f"({len(calls['shards'])} == {expected_shards})")
check("no shard exceeds SHARD_SIZE", all(len(s) <= ya.SHARD_SIZE for s in calls["shards"]))
check("no shard exceeds the filter id limit",
      all(len(s) <= ya.MAX_FILTER_IDS for s in calls["shards"]))
check("every id covered exactly once",
      sorted(i for s in calls["shards"] for i in s) == sorted(many))
ids = [r[0] for r in out]
check("merge deduplicates by video id", len(ids) == len(set(ids)))

print("\nOverlapping shards must not duplicate rows")
dupe_vids = [f"v{i}" for i in range(10)]
api, calls = make_api([row(f"v{i}") for i in range(ya.RESULT_CAP)],
                      {v: row(v) for v in dupe_vids})
out = api._fetch_video_rows(dupe_vids + dupe_vids, "2026-08-26")
check("duplicate ids collapse to one row each", len(out) == len(set(r[0] for r in out)))

print("\nEmpty response is passed through, not turned into a shard storm")
api, calls = make_api([], {})
out = api._fetch_video_rows(vids, "2026-08-26")
check("returns nothing", out == [])
check("does not shard on an empty result", calls["shards"] == [])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("All sharding tests passed.")
