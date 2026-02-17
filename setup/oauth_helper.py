"""One-time OAuth2 consent flow for YouTube Analytics API.

Run this locally after downloading client_secret.json from the GCP Console.
It opens a browser for you to sign in with the Google account that owns
the YouTube channel.

Usage:
    pip install google-auth-oauthlib
    python3 setup/oauth_helper.py

Prerequisites:
    - client_secret.json in the repo root (downloaded from GCP Console)
    - google-auth-oauthlib installed (pip install google-auth-oauthlib)
"""

import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dependency. Install it with:")
    print("  pip install google-auth-oauthlib")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]

# Look for client_secret.json in the repo root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CLIENT_SECRETS_FILE = os.path.join(REPO_ROOT, "client_secret.json")


def main() -> None:
    """Run the OAuth2 consent flow and print credentials."""
    if not os.path.exists(CLIENT_SECRETS_FILE):
        print(f"Error: {CLIENT_SECRETS_FILE} not found.")
        print("Download it from GCP Console > APIs & Services > Credentials")
        print("See setup/3_setup_oauth.sh for full instructions.")
        sys.exit(1)

    print("Starting OAuth2 consent flow...")
    print("A browser window will open. Sign in with your PERSONAL Google account")
    print("(the one that owns the YouTube channel).")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    credentials = flow.run_local_server(port=8080)

    print()
    print("=" * 50)
    print("  OAuth2 Setup Complete!")
    print("=" * 50)
    print()
    print(f"Client ID:     {credentials.client_id}")
    print(f"Client Secret: {credentials.client_secret}")
    print(f"Refresh Token: {credentials.refresh_token}")
    print()
    print("Store these in Secret Manager using the commands in setup/3_setup_oauth.sh")


if __name__ == "__main__":
    main()
