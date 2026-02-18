#!/usr/bin/env bash
set -euo pipefail

# YouTube Analytics API — OAuth2 Setup Guide
# This is a ONE-TIME setup that requires manual steps in the GCP Console.

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

cat << 'INSTRUCTIONS'
╔══════════════════════════════════════════════════════════════╗
║       YouTube Analytics API — OAuth2 Setup Guide            ║
╚══════════════════════════════════════════════════════════════╝

This is a ONE-TIME setup. Follow these steps:

STEP 1: Configure the OAuth Consent Screen
─────────────────────────────────────────────
  1. Go to: https://console.cloud.google.com/apis/credentials/consent
  2. Select "External" user type → Create
  3. Fill in:
     - App name: YouTube Analytics Pipeline
     - User support email: your email
     - Developer contact: your email
  4. Add scope: https://www.googleapis.com/auth/yt-analytics.readonly
  5. Add your PERSONAL Google account (the one that owns the
     YouTube channel) as a test user
  6. IMPORTANT: After testing, publish to "In production" to avoid
     7-day refresh token expiry. The unverified app warning is fine
     since only you will ever consent.

STEP 2: Create OAuth Credentials
─────────────────────────────────
  1. Go to: https://console.cloud.google.com/apis/credentials
  2. Click "Create Credentials" → "OAuth 2.0 Client ID"
  3. Application type: "Desktop app"
  4. Name: youtube-analytics-pipeline
  5. Download the JSON file → save as client_secret.json in this repo root
     (It's already in .gitignore, so it won't be committed)

STEP 3: Run the Consent Flow
─────────────────────────────
  pip install google-auth-oauthlib
  python3 setup/oauth_helper.py

  → A browser window will open
  → Sign in with your PERSONAL Google account (channel owner)
  → Approve the YouTube Analytics read-only access
  → The script will print your Client ID, Client Secret, and Refresh Token

STEP 4: Store Secrets in Secret Manager
────────────────────────────────────────
  Run the commands below (replacing values from Step 3):

INSTRUCTIONS

echo "  printf '%s' 'YOUR_CLIENT_ID' > /tmp/secret.tmp && \\"
echo "    gcloud secrets create youtube-oauth-client-id --data-file=/tmp/secret.tmp --project=$PROJECT_ID && \\"
echo "    rm /tmp/secret.tmp"
echo ""
echo "  printf '%s' 'YOUR_CLIENT_SECRET' > /tmp/secret.tmp && \\"
echo "    gcloud secrets create youtube-oauth-client-secret --data-file=/tmp/secret.tmp --project=$PROJECT_ID && \\"
echo "    rm /tmp/secret.tmp"
echo ""
echo "  printf '%s' 'YOUR_REFRESH_TOKEN' > /tmp/secret.tmp && \\"
echo "    gcloud secrets create youtube-oauth-refresh-token --data-file=/tmp/secret.tmp --project=$PROJECT_ID && \\"
echo "    rm /tmp/secret.tmp"
echo ""

cat << 'DONE'
STEP 5: Redeploy the Cloud Function
────────────────────────────────────
  bash setup/4_deploy_function.sh

  The function will now automatically load OAuth credentials from
  Secret Manager and fetch Analytics API data.

DONE
