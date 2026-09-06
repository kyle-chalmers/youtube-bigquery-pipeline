"""The view files are a contract: each states its grain, timezone and cardinality, names the
view it creates, and uses only the dataset placeholder (never a literal dataset)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VIEWS = sorted((ROOT / "sql" / "views").glob("*.sql"))
VERIFY_SQL = (ROOT / "sql" / "verification" / "phase3_views.sql").read_text()
RUNNER = (ROOT / "scripts" / "verify_views.sh").read_text()
VERIFY_TAGS = set(re.findall(r"^-- --(\w+)$", VERIFY_SQL, re.M))
# AVG over any source ratio or per-row average, with or without a table alias.
AVERAGED_RATIO = re.compile(r"AVG\(\s*(?:\w+\.)?\w*(?:_ctr|_rate|_percentage|_share|average_view_duration\w*)\s*\)", re.I)


def verify_block(tag):
    """The same extraction scripts/verify_views.sh performs: statement lines after the tag."""
    lines, on, started = [], False, False
    for line in VERIFY_SQL.splitlines():
        if line == f"-- --{tag}":
            on = True
            continue
        if on and re.match(r"^-- -{20,}", line):
            if started:
                break
            continue
        if on and not line.startswith("--"):
            started = True
            lines.append(line)
    return "\n".join(lines)


def test_there_are_view_files():
    assert len(VIEWS) >= 12


@pytest.mark.parametrize("path", VIEWS, ids=[p.name for p in VIEWS])
def test_view_header_contract(path):
    text = path.read_text()
    header = "\n".join(line for line in text.splitlines() if line.startswith("--"))
    assert re.search(r"^-- Grain:", header, re.M), "header must state the grain"
    assert "Cardinality" in header, "header must state join cardinality"
    assert "Timezone" in header, "header must state the timezone"
    assert "Source" in header, "header must name its sources"
    m = re.search(r"CREATE OR REPLACE VIEW `\$\{BQ_DATASET\}\.(\w+)` AS", text)
    assert m, "must be CREATE OR REPLACE VIEW on the ${BQ_DATASET} placeholder"
    view_name = m.group(1)
    assert path.stem.split("_", 1)[1] == view_name, "file name must match the view name"
    assert header.lstrip("- ").startswith(view_name + ":"), "first header line names the view"
    assert "youtube_analytics." not in text, "no literal dataset; use ${BQ_DATASET}"
    sql_only = "\n".join(l for l in text.splitlines() if not l.startswith("--"))
    for ref in re.findall(r"`([^`]+)`", sql_only):
        assert re.fullmatch(r"\$\{BQ_DATASET\}\.\w+", ref), f"every table reference must be ${{BQ_DATASET}}.<name>, got {ref}"
    for tag in re.findall(r"--(\w+)", header):
        if tag.startswith(("grain", "no_fanout", "funnel", "avd", "summary", "channel_level", "traffic_codes", "studio", "non_sub", "rolling", "type_split")):
            assert tag in VERIFY_TAGS, f"header cites --{tag}, which is not a block in phase3_views.sql"
    for literal in re.findall(r"video_type\s*=\s*'(\w+)'", text):
        assert literal in {"short", "full_length"}, f"unknown video_type literal {literal!r} (see youtube_data_api.classify_video_type)"
    assert "video_metadata`" not in text or view_name == "video_current", \
        "only video_current may read video_metadata directly (one row per video per day otherwise fans out)"


def test_ratio_columns_are_recomputed_not_averaged():
    """No view may AVG a source ratio column; ratios are recomputed from totals.
    channel_demographics is the documented exception (views_percentage has no total)."""
    for path in VIEWS:
        text = path.read_text()
        if path.stem.endswith("channel_demographics"):
            continue
        assert not AVERAGED_RATIO.search(text), path.name


def test_averaged_ratio_guard_catches_the_aliased_form():
    """The regex guards views that alias every source; a bare-name-only pattern was a no-op."""
    for bad in ["AVG(r.video_thumbnail_impressions_ctr)", "AVG( x_rate )", "avg(b.average_view_duration_seconds)", "AVG(views_percentage)"]:
        assert AVERAGED_RATIO.search(bad), bad
    assert not AVERAGED_RATIO.search("SAFE_DIVIDE(SUM(a * ctr), SUM(a))")


def test_grain_check_covers_exactly_the_view_files():
    names = set(re.findall(r"SELECT '(\w+)'(?: AS view_name)?, COUNT\(\*\)", verify_block("grain_checks")))
    assert names == {p.stem.split("_", 1)[1] for p in VIEWS}


def test_runner_blocks_and_columns_exist_in_the_sql():
    """Every block the runner asks for exists, extracts non-empty, and exposes every column
    the runner reads by name."""
    blocks = set(re.findall(r"block (\w+)", RUNNER))
    assert blocks and blocks <= VERIFY_TAGS, blocks - VERIFY_TAGS
    for tag in blocks:
        assert verify_block(tag).strip().endswith(";"), tag
    # col "$h" "$r" name  and  for c in a b c; do ... col "$h" "$r" $c
    named = set(re.findall(r'col "\$h" "\$r" (\w+)', RUNNER))
    for loop in re.findall(r"for c in ([\w ]+); do", RUNNER):
        named |= set(loop.split())
    all_aliases = set(re.findall(r"\bAS (\w+)", VERIFY_SQL))
    missing = named - all_aliases
    assert not missing, missing
