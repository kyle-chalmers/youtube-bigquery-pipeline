"""Shared retry policy for every Google API call in this pipeline.

Deliberately dependency-free (stdlib only) so importing it can never fail at
Cloud Function start. The analytics path in main.py is wrapped in an
`except ImportError` that skips analytics at INFO level; anything imported on that
path must therefore be something that cannot fail to import.

Retries 429 and the 5xx family. Retrying only 429 once dropped a video's traffic on
the first transient 500 in this repo, so the wider set is deliberate. The attempt
count is always the caller's decision: the Cloud Function uses 3, the backfill uses 5.

Transport-level exceptions (socket timeouts, connection resets) are NOT retried.
That is the pre-existing behaviour, pinned by tests/test_retry.py; widening it is a
Phase 4 hardening decision, not something to change silently here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def http_status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from a googleapiclient HttpError or a requests HTTPError.

    googleapiclient.errors.HttpError carries `resp.status`; requests.HTTPError carries
    `response.status_code`. Anything else returns None and is treated as non-retryable.
    """
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def with_retry(callable_fn: Any, max_retries: int = 3) -> Any:
    """Run an API call, retrying transient HTTP failures with exponential backoff.

    Args:
        callable_fn: Zero-argument callable that performs the request.
        max_retries: Retries after the first attempt. 3 means up to 4 attempts.
    """
    for attempt in range(max_retries + 1):
        try:
            return callable_fn()
        except Exception as e:  # noqa: BLE001 - we re-raise anything we do not recognise
            status = http_status_of(e)
            if status in RETRYABLE_STATUSES and attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    f"HTTP {status}, retrying in {wait}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Unreachable")
