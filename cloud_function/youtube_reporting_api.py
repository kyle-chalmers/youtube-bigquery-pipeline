"""YouTube Reporting API v1 client: jobs, report listings, report downloads.

The Reporting API is a bulk-report system. You create a job per report type once;
YouTube then generates one CSV per Pacific-time day and keeps it for 60 days (30 for
the historical backfill it generates when a job is first created). Facts that shape
this client, all observed against this channel on 2026-09-05:

- A day can be regenerated. Day 2026-08-31 was first generated 09-02 and again 09-05.
  Both reports share startTime/endTime; only the newest createTime is authoritative.
- Column order in the CSV is not stable. Parse by header, never by position. Parsing
  is added in Phase 2 as parse_report(); this module lists and downloads only.
- Days with no activity still produce a report containing only the header row, so a
  zero-byte body is never legitimate and is rejected here.
- Reports that age past retention simply vanish from jobs.reports.list.

Nothing here writes to BigQuery.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from retry import RETRYABLE_STATUSES, with_retry  # noqa: F401  (re-exported for callers)

logger = logging.getLogger(__name__)

MAX_REPORT_BYTES = 50 * 1024 * 1024  # decompressed; real reports are tens of KB
MAX_PAGES = 1000  # a paginator guard; the API never needs more than a handful


class ReportDownloadError(RuntimeError):
    """A report body was empty, truncated, or not a CSV."""


@dataclass(frozen=True)
class ReportRef:
    """One entry from jobs.reports.list. Immutable so it is safe as a dict key."""

    report_id: str
    job_id: str
    report_type: str
    start_time: datetime
    end_time: datetime
    create_time: datetime
    download_url: str

    @property
    def report_date(self) -> str:
        """The Pacific-time day this report covers, as YYYY-MM-DD.

        startTime is midnight Pacific expressed in UTC (07:00Z or 08:00Z depending on
        daylight saving), so the UTC date of startTime is the report day.
        """
        return self.start_time.strftime("%Y-%m-%d")


def _parse_rfc3339(value: str) -> datetime:
    """Parse the API's RFC3339 'Zulu' timestamps, which may carry microseconds."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _paginate(list_call: Any, key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Follow nextPageToken until exhausted, guarding against a stuck or runaway paginator."""
    items: list[dict[str, Any]] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_PAGES):
        params = dict(kwargs)
        if token:
            params["pageToken"] = token
        resp = with_retry(lambda p=params: list_call(**p).execute())
        items.extend(resp.get(key, []))
        token = resp.get("nextPageToken")
        if not token:
            return items
        if token in seen_tokens:
            raise RuntimeError(f"pagination returned the same nextPageToken twice ({key})")
        seen_tokens.add(token)
    raise RuntimeError(f"pagination exceeded {MAX_PAGES} pages ({key})")


class YouTubeReportingClient:
    """Thin wrapper over the youtubereporting v1 discovery client."""

    def __init__(self, credentials: Any, max_retries: int = 3) -> None:
        self._credentials = credentials
        self.max_retries = max_retries
        self.service = build(
            "youtubereporting", "v1", credentials=credentials, cache_discovery=False
        )
        self._session: AuthorizedSession | None = None

    def list_report_types(self) -> list[dict[str, Any]]:
        """Report types this channel may create jobs for (system-managed excluded)."""
        types = _paginate(self.service.reportTypes().list, "reportTypes")
        return [t for t in types if not t.get("systemManaged")]

    def list_jobs(self) -> list[dict[str, Any]]:
        """All non-system-managed jobs for the authenticated channel."""
        jobs = _paginate(self.service.jobs().list, "jobs")
        return [j for j in jobs if not j.get("systemManaged")]

    def create_job(self, name: str, report_type_id: str) -> dict[str, Any]:
        """Create a reporting job. Raises HttpError 409 if one already exists.

        Creation is not idempotent, but the API refuses a second job for the same report
        type with HTTP 409, so a retry after a 5xx that actually created the job surfaces
        as 409 and callers treat that as "exists".
        """
        body = {"name": name, "reportTypeId": report_type_id}
        return with_retry(
            lambda: self.service.jobs().create(body=body).execute(), self.max_retries
        )

    def list_reports(
        self,
        job_id: str,
        report_type: str,
        created_after: datetime | None = None,
    ) -> list[ReportRef]:
        """Every retained report for a job, following pagination.

        Sorted by (start_time, create_time) so callers can find the newest generation
        of each day by taking the last entry per report_date.
        """
        kwargs: dict[str, Any] = {"jobId": job_id, "pageSize": 200}
        if created_after is not None:
            if created_after.tzinfo is None:
                raise ValueError("created_after must be timezone-aware (UTC)")
            kwargs["createdAfter"] = created_after.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        raw = _paginate(self.service.jobs().reports().list, "reports", **kwargs)
        refs = [
            ReportRef(
                report_id=r["id"],
                job_id=r["jobId"],
                report_type=report_type,
                start_time=_parse_rfc3339(r["startTime"]),
                end_time=_parse_rfc3339(r["endTime"]),
                create_time=_parse_rfc3339(r["createTime"]),
                download_url=r["downloadUrl"],
            )
            for r in raw
        ]
        refs.sort(key=lambda x: (x.start_time, x.create_time))
        return refs

    def download(self, ref: ReportRef, max_bytes: int = MAX_REPORT_BYTES) -> bytes:
        """Fetch a report body as decompressed CSV bytes.

        Retries transient HTTP statuses with the shared policy. Rejects an empty body, a
        body whose length disagrees with Content-Length (a truncated read), a first line
        that is not a CSV header, and a body over max_bytes. The download URL and bearer
        token are never logged.

        The cap is checked after the body is read; it bounds what reaches the parser and
        BigQuery, it does not bound peak memory. Real reports are tens of kilobytes.
        """
        if self._session is None:
            self._session = AuthorizedSession(self._credentials)

        def _get() -> Any:
            resp = self._session.get(  # type: ignore[union-attr]
                ref.download_url, headers={"Accept-Encoding": "gzip"}, timeout=120
            )
            resp.raise_for_status()
            return resp

        try:
            resp = with_retry(_get, self.max_retries)
        except Exception as e:  # noqa: BLE001 - requests exceptions carry the URL; never let it out
            status = getattr(getattr(e, "response", None), "status_code", None)
            raise ReportDownloadError(
                f"report {ref.report_id}: download failed with HTTP {status if status else type(e).__name__}"
            ) from None
        data = resp.content
        declared = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
        if declared and resp.headers.get("Content-Encoding") is None and int(declared) != len(data):
            raise ReportDownloadError(
                f"report {ref.report_id}: Content-Length {declared} but read {len(data)} bytes"
            )
        if data[:2] == b"\x1f\x8b":  # gzip magic; requests usually inflates, but not always
            data = gzip.decompress(data)
        if not data:
            raise ReportDownloadError(f"report {ref.report_id}: empty body")
        first_line = data.split(b"\n", 1)[0]
        if b"," not in first_line:
            raise ReportDownloadError(f"report {ref.report_id}: first line is not a CSV header")
        if len(data) > max_bytes:
            raise ReportDownloadError(
                f"report {ref.report_id} is {len(data)} bytes, over the {max_bytes} cap"
            )
        return data


def newest_per_day(refs: list[ReportRef]) -> dict[str, ReportRef]:
    """Collapse a job's reports to the newest generation of each report day.

    Google's rule: when two reports share a time window, import only the one with the
    newer createTime. Returns {report_date: ReportRef}. On an exact createTime tie the
    first seen wins; ties have not been observed and would mean two reports generated
    in the same microsecond.
    """
    newest: dict[str, ReportRef] = {}
    for ref in refs:
        current = newest.get(ref.report_date)
        if current is None or ref.create_time > current.create_time:
            newest[ref.report_date] = ref
    return newest
