"""OAuth2 credentials for the YouTube Analytics and Reporting APIs.

One loader, shared by the Cloud Function clients and the setup/ scripts. Before this
module existed the same three Secret Manager reads and Credentials construction were
copied into youtube_analytics_api.py, setup/backfill_analytics.py and an internal probe,
and the copies had already drifted (different retry counts, a hardcoded project id).

The refresh token was issued by the channel-owning Google account with the
yt-analytics.readonly scope. The Reporting API accepts that same scope, so both APIs
share one credential and no second consent is needed.
"""

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials

SECRET_CLIENT_ID = "youtube-oauth-client-id"
SECRET_CLIENT_SECRET = "youtube-oauth-client-secret"
SECRET_REFRESH_TOKEN = "youtube-oauth-refresh-token"

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]


def get_secret(project_id: str, secret_id: str) -> str:
    """Read the latest version of a Secret Manager secret as text."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def load_oauth_credentials(project_id: str) -> Credentials:
    """Build auto-refreshing OAuth2 credentials from the three stored secrets.

    Args:
        project_id: GCP project that holds the secrets.

    Returns:
        Credentials that refresh themselves from the stored refresh token.
    """
    return Credentials(
        token=None,
        refresh_token=get_secret(project_id, SECRET_REFRESH_TOKEN),
        client_id=get_secret(project_id, SECRET_CLIENT_ID),
        client_secret=get_secret(project_id, SECRET_CLIENT_SECRET),
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
