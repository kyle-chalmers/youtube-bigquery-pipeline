"""Local setup tools can refresh the active gcloud token without writing credentials."""

import _bootstrap
import gcloud_credentials


def test_explicit_gcloud_access_token_becomes_refreshable_credentials(monkeypatch):
    monkeypatch.setenv("GCLOUD_ACCESS_TOKEN", "test-token")
    credentials = _bootstrap.google_cloud_credentials()
    assert credentials.token == "test-token"
    assert credentials.expiry is not None


def test_gcloud_credentials_refresh_by_minting_a_new_cli_token(monkeypatch):
    monkeypatch.setenv("GCLOUD_ACCESS_TOKEN", "old-token")
    credentials = _bootstrap.google_cloud_credentials()

    class Result:
        stdout = "new-token\n"

    monkeypatch.setattr(gcloud_credentials.subprocess, "run", lambda *args, **kwargs: Result())
    credentials.refresh(None)
    assert credentials.token == "new-token"
