"""Tell GitHub that a weekly Analytics refresh finished, so downstream checks run on fresh data.

Sends one repository_dispatch event (POST /repos/{owner}/{repo}/dispatches) of type
youtube-refresh-complete. The receiving repo's workflow (for example the metric canary in
youtube-semantic-layer) listens for it and keeps its own cron as a fallback, so this call is
best effort: a failure is logged as a warning and never changes the refresh's result.

Configured by two environment variables, both optional:
    GITHUB_DISPATCH_REPO   owner/repo to notify; unset means do nothing (staging, forks)
    GITHUB_DISPATCH_TOKEN  bound from Secret Manager at deploy time. A fine-grained personal
                           access token limited to that one repository with Contents: write,
                           the minimum GitHub lists for this endpoint.
The token is never logged.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

import requests

from log_safety import redact

EVENT_TYPE = "youtube-refresh-complete"
TIMEOUT_SECONDS = 10

SENT_LOG = "GitHub dispatch sent"
FAILED_LOG = "GitHub dispatch failed"
NOT_CONFIGURED_LOG = "GitHub dispatch not configured"


def _scrub(text: str, token: str) -> str:
    return redact(text.replace(token, "[REDACTED]") if token else text)


def notify_refresh_complete(
    refresh_run_id: str,
    log: logging.LoggerAdapter,
    repo: str | None = None,
    token: str | None = None,
    post: Callable = requests.post,
) -> bool:
    """Send the dispatch. Returns True when GitHub accepted it (204), False otherwise."""
    repo = os.environ.get("GITHUB_DISPATCH_REPO", "") if repo is None else repo
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "") if token is None else token
    if not repo or not token:
        log.info(f"{NOT_CONFIGURED_LOG}; set GITHUB_DISPATCH_REPO and GITHUB_DISPATCH_TOKEN to enable")
        return False

    try:
        response = post(
            f"https://api.github.com/repos/{repo}/dispatches",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"event_type": EVENT_TYPE, "client_payload": {"refresh_run_id": refresh_run_id}},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        log.warning(f"{FAILED_LOG}: {type(e).__name__}: {_scrub(str(e), token)}")
        return False

    if response.status_code != 204:
        log.warning(f"{FAILED_LOG}: HTTP {response.status_code}: {_scrub(response.text[:300], token)}")
        return False
    log.info(f"{SENT_LOG}: repo={repo} event_type={EVENT_TYPE}")
    return True
