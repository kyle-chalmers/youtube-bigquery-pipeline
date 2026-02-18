<objective>
Add structured JSON logging to the Cloud Function so that application-level logs (pipeline progress, row counts, errors) appear in Google Cloud Logging and are easily queryable via `gcloud logging read` or the Cloud Logging console.

Currently, `main.py` uses Python's `logging.basicConfig()` which writes plain text to stderr. Cloud Run captures this, but the logs don't appear in standard Cloud Logging filters or in `gcloud functions logs read` output. Only infrastructure logs (startup probes, HTTP status codes) are visible — the actual pipeline execution details are invisible.
</objective>

<context>
Read `./CLAUDE.md` for project conventions before making changes.

Key files to examine:
- `./cloud_function/main.py` — entry point, currently uses `logging.basicConfig(level=logging.INFO)`
- `./cloud_function/youtube_data_api.py` — Data API client, may use logging
- `./cloud_function/youtube_analytics_api.py` — Analytics API client, uses logging
- `./cloud_function/bigquery_writer.py` — BigQuery writer, may use logging
- `./cloud_function/requirements.txt` — current dependencies

This is a Google Cloud Function (2nd gen) running Python 3.11 on Cloud Run.
The function is triggered daily at 11:50 PM Phoenix time by Cloud Scheduler.
The pipeline processes ~64 YouTube videos across 4 BigQuery tables.

The goal is that next time someone runs `gcloud functions logs read youtube-bigquery-pipeline --region=us-central1 --gen2 --limit=50`, they see structured, meaningful application logs — not just HTTP status codes and startup messages.
</context>

<requirements>
1. Replace `logging.basicConfig()` in `main.py` with Google Cloud Logging's structured logging integration for Cloud Run. In Cloud Functions 2nd gen (Cloud Run), the recommended approach is to write structured JSON to stdout, which Cloud Logging automatically parses.

2. Use `google-cloud-logging` library's Cloud Run integration. The simplest approach:
   ```python
   import google.cloud.logging
   client = google.cloud.logging.Client()
   client.setup_logging()
   ```
   This redirects Python's standard `logging` module to Cloud Logging, so all existing `logger.info()`, `logger.warning()`, `logger.exception()` calls automatically become structured logs.

3. Add `google-cloud-logging` to `requirements.txt`.

4. Ensure the following events are logged at appropriate levels:
   - INFO: Pipeline start (with snapshot_date)
   - INFO: Video count after playlist fetch
   - INFO: Video details fetched count
   - INFO: Each BigQuery table write (table name + row count)
   - INFO: Pipeline completion summary (total videos, shorts, full_length, all row counts)
   - WARNING: Analytics API partial failures (individual video errors)
   - ERROR: Pipeline failure (full exception with traceback)

5. Include a `pipeline_run_id` or similar correlation field in log entries so all logs from a single execution can be filtered together. A simple approach: generate a UUID at request start and include it via `logging.LoggerAdapter` or in the structured log extra fields.

6. Do NOT break the existing HTTP response format — the function must still return the summary JSON dict with 200/500 status codes.

7. Do NOT add excessive logging. Keep it concise — one log per major step, not per-video or per-API-call. The goal is a quick health check, not a debug trace.
</requirements>

<implementation>
Approach:
- Modify `main.py` to initialize `google.cloud.logging` at module level
- Keep using Python's standard `logging` module for all log calls (the library redirects it)
- Add a `run_id` to log entries for correlation
- Review other modules (`youtube_data_api.py`, `youtube_analytics_api.py`, `bigquery_writer.py`) — if they already use `logging.getLogger()`, their logs will automatically be captured. Only add new log lines if major steps are missing.
- Add `google-cloud-logging` to `requirements.txt`

Why structured JSON logging matters: Cloud Logging can parse JSON log entries and index fields like severity, message, and custom labels. This makes them filterable in the console and via `gcloud logging read` with expressions like `jsonPayload.run_id="abc123"`. Plain text stderr logs get lumped into a generic textPayload with no structure.

What to avoid:
- Don't use `print()` — use `logging` module consistently
- Don't add per-video logging (63+ log entries per run would be noisy)
- Don't change the function's HTTP response contract
- Don't add `google-cloud-logging` initialization in a way that breaks local testing — wrap the Cloud Logging setup in a try/except so the function still works locally with basic stderr logging
</implementation>

<output>
Modify these files:
- `./cloud_function/main.py` — structured logging setup + run_id correlation
- `./cloud_function/requirements.txt` — add `google-cloud-logging`

Optionally touch (only if logging gaps exist):
- `./cloud_function/bigquery_writer.py`
- `./cloud_function/youtube_data_api.py`
- `./cloud_function/youtube_analytics_api.py`
</output>

<verification>
Before declaring complete, verify:
1. `python3 -c "import py_compile; py_compile.compile('./cloud_function/main.py', doraise=True); print('syntax OK')"` passes
2. `google-cloud-logging` is listed in `./cloud_function/requirements.txt`
3. The logging setup is wrapped in try/except so local runs still work
4. The function still returns the same JSON response format (snapshot_date, videos_processed, shorts, full_length, rows_inserted, analytics_errors)
5. No per-video or per-API-call logging was added — only major pipeline steps
6. A run_id or similar correlation field is included for filtering
</verification>

<success_criteria>
- After deploying, `gcloud functions logs read youtube-bigquery-pipeline --region=us-central1 --gen2 --limit=20` shows structured application logs including: pipeline start, video count, table writes with row counts, and pipeline completion summary
- Logs can be filtered by run_id to isolate a single execution
- No regressions in function behavior — same HTTP response, same data written to BigQuery
</success_criteria>
