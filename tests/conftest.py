"""Shared pytest setup: import paths, the environment main.py reads at import time, and a
stub for google.cloud.logging so importing main does not probe for credentials.

Tests are offline. They never touch Secret Manager, BigQuery, GCS, or YouTube; every
client is replaced with a fake in the test that uses it.
"""

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud_function"))
sys.path.insert(0, str(ROOT / "setup"))

os.environ.setdefault("GCP_PROJECT", "test-project")
os.environ.setdefault("YOUTUBE_CHANNEL_ID", "UCtestchannel000000000000")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("YOUTUBE_API_KEY", "test-key")

# main.py calls google.cloud.logging.Client().setup_logging() at import. With no ADC that
# probes the metadata server for several seconds before falling back. Stub it so tests
# import main instantly and never reach for credentials.
_fake_logging = types.ModuleType("google.cloud.logging")


class _FakeLoggingClient:
    def __init__(self, *a, **k):
        pass

    def setup_logging(self):
        return None


_fake_logging.Client = _FakeLoggingClient
sys.modules.setdefault("google.cloud.logging", _fake_logging)
