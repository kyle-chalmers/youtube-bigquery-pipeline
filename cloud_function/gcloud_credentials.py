"""Refreshable local credentials backed by the active gcloud CLI session."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials


def _token_expiry() -> datetime:
    return datetime.utcnow() + timedelta(minutes=50)


def _mint_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud returned an empty access token")
    return token


def credentials_from_environment() -> Credentials | None:
    """Return refreshable CLI credentials when GCLOUD_ACCESS_TOKEN is set."""
    token = os.environ.get("GCLOUD_ACCESS_TOKEN")
    if not token:
        return None

    def refresh_handler(request, scopes=None):
        del request, scopes
        return _mint_token(), _token_expiry()

    return Credentials(token=token, expiry=_token_expiry(), refresh_handler=refresh_handler)
