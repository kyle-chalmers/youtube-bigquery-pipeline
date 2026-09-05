"""The single credential loader every client and script now depends on."""

import oauth_credentials as oc


class FakeSecretClient:
    def __init__(self, values):
        self.values = values
        self.requested = []

    def access_secret_version(self, request):
        self.requested.append(request["name"])
        secret = request["name"].split("/secrets/")[1].split("/")[0]

        class _Resp:
            class payload:  # noqa: N801 - mirrors the client
                data = self.values[secret].encode()

        return _Resp()


def test_loads_the_three_named_secrets_latest_versions(monkeypatch):
    fake = FakeSecretClient({
        "youtube-oauth-client-id": "cid",
        "youtube-oauth-client-secret": "csec",
        "youtube-oauth-refresh-token": "rtok",
    })
    monkeypatch.setattr(oc.secretmanager, "SecretManagerServiceClient", lambda: fake)
    creds = oc.load_oauth_credentials("proj-1")
    assert sorted(fake.requested) == sorted([
        "projects/proj-1/secrets/youtube-oauth-client-id/versions/latest",
        "projects/proj-1/secrets/youtube-oauth-client-secret/versions/latest",
        "projects/proj-1/secrets/youtube-oauth-refresh-token/versions/latest",
    ])
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"
    assert creds.refresh_token == "rtok"
    assert creds.token is None, "no access token yet; the first call refreshes"
    assert creds.token_uri == "https://oauth2.googleapis.com/token"
    assert creds.scopes == ["https://www.googleapis.com/auth/yt-analytics.readonly"]


def test_scope_is_the_single_analytics_scope_shared_with_the_reporting_api():
    # The Reporting API accepts yt-analytics.readonly, so one credential serves both.
    assert oc.SCOPES == ["https://www.googleapis.com/auth/yt-analytics.readonly"]
