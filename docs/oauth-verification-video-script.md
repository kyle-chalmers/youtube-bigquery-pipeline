# OAuth Verification Video Script

Use this script to record the unlisted YouTube video required for Google OAuth sensitive scope verification.

The video must show:

- The app name: `YouTube Analytics Pipeline`
- The OAuth consent screen in English
- The browser address bar on the OAuth consent screen, with the OAuth client ID visible in the URL
- The exact scope being requested: `yt-analytics.readonly`
- The app using the granted scope to read YouTube Analytics data and write it to BigQuery

## Setup Before Recording

- Open this repo in a terminal.
- Open the OAuth consent flow helper: `setup/oauth_helper.py`.
- Open the Google Cloud Console pages for BigQuery and Cloud Functions.
- Make sure the browser zoom level leaves the address bar, consent screen app name, and requested scope readable.
- Do not show client secrets, refresh tokens, Secret Manager secret values, API keys, or `.internal/` files.

## Shot List and Narration

### 1. App Identity

**Screen:** GitHub Pages homepage at `https://kyle-chalmers.github.io/youtube-bigquery-pipeline/`.

**Narration:**

> This is YouTube Analytics Pipeline. It is a daily pipeline that snapshots analytics from my own YouTube channel into BigQuery for historical analysis.

### 2. Data Access Explanation

**Screen:** Privacy Policy page at `https://kyle-chalmers.github.io/youtube-bigquery-pipeline/privacy.html`.

**Narration:**

> The app requests read-only YouTube Analytics access through the `yt-analytics.readonly` scope. It reads watch time, average view duration, average view percentage, subscriber changes, shares, and traffic source breakdowns. It does not write to YouTube and it does not access unrelated Google account data.

### 3. Start OAuth Grant

**Screen:** Terminal showing `python3 setup/oauth_helper.py`.

**Narration:**

> Here is how a user grants access. I run the local OAuth helper from the repository. This opens Google's OAuth flow in the browser.

### 4. Consent Screen

**Screen:** Browser at `accounts.google.com` showing the consent screen.

Keep visible:

- Browser address bar
- OAuth client ID in the URL
- App name `YouTube Analytics Pipeline`
- Requested `yt-analytics.readonly` scope or its Google consent-screen description

**Narration:**

> The consent screen shows the app name, YouTube Analytics Pipeline. The browser URL includes the OAuth client ID for this app. The permission requested is read-only access to YouTube Analytics reports. I approve this request for the Google account that owns my YouTube channel.

### 5. OAuth Result

**Screen:** Terminal showing that the OAuth helper completed. Keep secrets hidden or cropped.

**Narration:**

> The OAuth helper receives a refresh token. In the deployed pipeline, the OAuth credentials are stored in Google Secret Manager, not in source code.

### 6. Scope Usage in the App

**Screen:** Trigger the deployed Cloud Function with curl, then show the JSON response and BigQuery tables. Make sure `daily_video_analytics` or `daily_traffic_sources` has rows greater than zero.

**Narration:**

> The Cloud Function uses the granted read-only scope to call the YouTube Analytics API. It writes the returned analytics data into BigQuery tables. This is the only use of the scope.

### 7. Stored Data

**Screen:** BigQuery table preview for `daily_video_analytics` or `daily_traffic_sources`. Show column names and recent rows, but avoid exposing anything you do not want in the submission video.

**Narration:**

> The stored data is channel analytics for my own YouTube content. The dataset is used for historical trend analysis in BigQuery.

### 8. Close

**Screen:** Privacy Policy page again.

**Narration:**

> The homepage and privacy policy describe the app, the scope, how the data is used, where it is stored, how it can be deleted, and how to contact the developer.

## Quick Checklist Before Upload

- Set YouTube visibility to **Unlisted**.
- Confirm the consent screen app name is readable.
- Confirm the OAuth client ID is visible in the browser URL bar.
- Confirm the requested scope is visible or clearly represented by Google's permission text.
- Confirm the app's use of the resulting analytics data is shown in BigQuery.
- Confirm no secrets or private notes are visible.
