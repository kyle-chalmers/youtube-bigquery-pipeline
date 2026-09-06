"""Credentials never reach a log line. The Data API key rides in every request URL, and
googleapiclient embeds that URL in HttpError messages and tracebacks."""

import logging
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import log_safety
import main


def test_redact_strips_api_key_and_tokens_but_keeps_the_rest():
    text = ('<HttpError 403 when requesting https://youtube.googleapis.com/youtube/v3/playlistItems'
            '?part=contentDetails&playlistId=UUabc&maxResults=50&key=AIza-EXAMPLE-NOT-A-REAL-KEY&alt=json '
            'returned "quotaExceeded">, header Authorization: Bearer ya29.a0AfH6SMBx-token_value')
    out = log_safety.redact(text)
    assert "AIza" not in out and "ya29" not in out
    assert "key=<redacted>&alt=json" in out
    assert "Bearer <redacted>" in out
    assert "playlistId=UUabc" in out and "quotaExceeded" in out


def test_no_raw_exception_text_reaches_a_log_line_anywhere_in_cloud_function():
    """CLAUDE.md rule: every log line built from an exception goes through redact. No
    log.exception (it prints the raw traceback) and no f-string that interpolates {e} bare."""
    for path in (ROOT / "cloud_function").glob("*.py"):
        text = path.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("#", "\"\"\"", "API key", "`log")))
        assert ".exception(" not in code, f"{path.name} uses log.exception; use log.error with redact(traceback.format_exc())"
        for m in re.finditer(r"\.(?:error|warning|info|critical)\(f\"[^\"]*\{e\}", code):
            raise AssertionError(f"{path.name}: raw {{e}} in a log call: {m.group(0)}")


def test_redact_leaves_plain_text_alone():
    assert log_safety.redact("Pipeline failed — connection reset") == "Pipeline failed — connection reset"


def test_pipeline_failure_log_carries_no_api_key(monkeypatch, caplog):
    class Boom:
        def __init__(self, *a, **k):
            pass

        def get_all_video_ids(self):
            raise RuntimeError("HttpError 403 https://x/y?key=AIzaSECRETVALUE123&alt=json quotaExceeded")

    monkeypatch.setattr(main, "YouTubeDataAPI", Boom)
    monkeypatch.setattr(main, "BigQueryWriter", lambda **k: None)
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSECRETVALUE123")

    class Req:
        args = {}

        def get_json(self, silent=True):
            return None

    with caplog.at_level(logging.ERROR):
        body, status = main.main(Req())
    assert status == 500
    joined = "\n".join(r.getMessage() for r in caplog.records) + str(body)
    assert "AIzaSECRETVALUE123" not in joined
    assert "quotaExceeded" in joined
    assert any(r.getMessage().startswith("Pipeline failed") for r in caplog.records), "alert string must survive"
