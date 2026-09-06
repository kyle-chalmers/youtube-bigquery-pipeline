#!/usr/bin/env python3
"""Archive every retained YouTube Reporting API report to a Cloud Storage bucket.

YouTube keeps a report for 60 days (30 for backfill reports) and then it is gone. This
script is the "download and persist" step Google's docs ask for: it lists every report
on every job and copies each one it has not seen into

    gs://<bucket>/<report_type>/<report_date>/<report_id>.csv.gz

Objects are created only if absent, so re-running is safe and cheap. Each object carries
metadata (job_id, create_time, start_time, sha256 of the CSV, csv_bytes, data_rows) so a
later loader can replay from the archive without asking YouTube.

    python3 setup/archive_reporting_raw.py --dry-run           # list what would be archived
    python3 setup/archive_reporting_raw.py                     # archive, creating the bucket if needed
    python3 setup/archive_reporting_raw.py --verify            # also re-download one archived report and compare
    python3 setup/archive_reporting_raw.py --verify-report ID  # verify a specific report id

Exit status is 1 whenever the archive is not provably complete: zero jobs or zero
reports listed (a scope or account problem, not an empty channel), any report that
failed to download or upload, any listed report absent from the bucket afterwards, or a
verify mismatch. If you pipe the output through another command, use `set -o pipefail`
or read the final RESULT line, or the exit status you see is the pipe's.

Bucket default: <GCP_PROJECT>-youtube-reporting-raw in GCP_REGION (us-central1), uniform
bucket-level access and public access prevention enforced, on an existing bucket too.
Touches no BigQuery resource.
"""

import argparse
import gzip
import hashlib
import os
import sys

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

import _bootstrap  # noqa: F401  (adds cloud_function/ to sys.path)

# isort: split   (everything below needs _bootstrap to have run first)
from oauth_credentials import load_oauth_credentials
from youtube_reporting_api import ReportRef, YouTubeReportingClient


def object_name(ref: ReportRef) -> str:
    """The archive's path contract: report_type/report_date/report_id.csv.gz."""
    return f"{ref.report_type}/{ref.report_date}/{ref.report_id}.csv.gz"


def csv_data_rows(csv_bytes: bytes) -> int:
    """Data rows in a CSV body (header excluded), correct with or without a trailing newline."""
    return max(len(csv_bytes.splitlines()) - 1, 0)


def ensure_bucket(client: storage.Client, name: str, location: str, dry_run: bool) -> storage.Bucket:
    """Get or create the bucket and make sure it is locked down either way."""
    try:
        bucket = client.get_bucket(name)
        exists = True
    except NotFound:
        exists = False
        bucket = client.bucket(name)

    if not exists:
        if dry_run:
            print(f"[dry-run] would create bucket gs://{name} in {location}")
            return bucket
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket.iam_configuration.public_access_prevention = "enforced"
        bucket = client.create_bucket(bucket, location=location)
        print(f"created bucket gs://{name} in {location} (uniform access, public access blocked)")
        return bucket

    cfg = bucket.iam_configuration
    hardened = cfg.uniform_bucket_level_access_enabled and cfg.public_access_prevention == "enforced"
    print(f"bucket gs://{name} exists (location={bucket.location}, hardened={bool(hardened)})")
    if not hardened:
        if dry_run:
            print("[dry-run] would enforce uniform access and public access prevention")
        else:
            cfg.uniform_bucket_level_access_enabled = True
            cfg.public_access_prevention = "enforced"
            bucket.patch()
            bucket.reload()
            cfg = bucket.iam_configuration
            if not (cfg.uniform_bucket_level_access_enabled and cfg.public_access_prevention == "enforced"):
                sys.exit(f"refusing to upload: could not harden gs://{name}")
            print("enforced uniform access and public access prevention on the existing bucket")
    return bucket


def archive_one(bucket: storage.Bucket, yt: YouTubeReportingClient, ref: ReportRef) -> tuple[str, int, str]:
    """Download one report and store it gzipped, create-if-absent.

    Returns (status, csv_bytes, sha256) where status is 'archived' or 'exists'.
    After an upload the blob is reloaded and its stored hash metadata compared to the
    local hash, so "archived" means "stored and read back", not "upload call returned".
    """
    blob = bucket.blob(object_name(ref))
    csv_bytes = yt.download(ref)
    sha = hashlib.sha256(csv_bytes).hexdigest()
    blob.metadata = {
        "job_id": ref.job_id,
        "report_id": ref.report_id,
        "report_type": ref.report_type,
        "report_date": ref.report_date,
        "start_time": ref.start_time.isoformat(),
        "end_time": ref.end_time.isoformat(),
        "create_time": ref.create_time.isoformat(),
        "csv_sha256": sha,
        "csv_bytes": str(len(csv_bytes)),
        "data_rows": str(csv_data_rows(csv_bytes)),
    }
    blob.content_encoding = "gzip"
    try:
        blob.upload_from_string(
            gzip.compress(csv_bytes, mtime=0),
            content_type="text/csv",
            if_generation_match=0,
        )
    except PreconditionFailed:
        return "exists", len(csv_bytes), sha
    blob.reload()
    stored = (blob.metadata or {}).get("csv_sha256")
    if stored != sha:
        raise RuntimeError(f"{object_name(ref)}: stored sha256 {stored} != local {sha}")
    return "archived", len(csv_bytes), sha


def verify_one(bucket: storage.Bucket, yt: YouTubeReportingClient, ref: ReportRef) -> bool:
    """Compare the archived bytes for one report against a fresh API download."""
    blob = bucket.blob(object_name(ref))
    stored = gzip.decompress(blob.download_as_bytes(raw_download=True))
    fresh = yt.download(ref)
    same = hashlib.sha256(stored).hexdigest() == hashlib.sha256(fresh).hexdigest()
    print(
        f"verify {object_name(ref)}: stored {len(stored)} bytes, fresh {len(fresh)} bytes, "
        f"sha256 {'MATCH' if same else 'MISMATCH'}"
    )
    return same


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="re-download one archived report and compare")
    parser.add_argument("--verify-report", default=None, help="report id to verify (implies --verify)")
    parser.add_argument("--job", default=None, help="restrict to one job id")
    args = parser.parse_args()

    project = _bootstrap.resolve_project()
    location = os.environ.get("GCP_REGION", "us-central1")
    bucket_name = args.bucket or f"{project}-youtube-reporting-raw"

    yt = YouTubeReportingClient(load_oauth_credentials(project))
    gcs = storage.Client(project=project)
    bucket = ensure_bucket(gcs, bucket_name, location, args.dry_run)

    existing: set[str] = set()
    if bucket.exists():
        existing = {b.name for b in gcs.list_blobs(bucket_name)}
    print(f"{len(existing)} object(s) already in gs://{bucket_name}\n")

    jobs = yt.list_jobs()
    if args.job:
        jobs = [j for j in jobs if j["id"] == args.job]
    if not jobs:
        print("RESULT: FAIL  no reporting jobs listed (scope, account, or --job filter problem)", file=sys.stderr)
        return 1

    grand = {"reports": 0, "archived": 0, "exists": 0, "would": 0, "failed": 0, "bytes": 0}
    all_refs: list[ReportRef] = []
    failures: list[str] = []
    for job in jobs:
        refs = yt.list_reports(job["id"], job["reportTypeId"])
        all_refs.extend(refs)
        counts = {"archived": 0, "exists": 0, "would": 0, "failed": 0}
        for ref in refs:
            grand["reports"] += 1
            if object_name(ref) in existing:
                counts["exists"] += 1
                continue
            if args.dry_run:
                counts["would"] += 1
                continue
            try:
                status, nbytes, _ = archive_one(bucket, yt, ref)
            except Exception as e:  # noqa: BLE001 - one bad report must not hide the rest
                counts["failed"] += 1
                failures.append(f"{object_name(ref)}: {type(e).__name__}: {e}")
                continue
            counts[status] += 1
            grand["bytes"] += nbytes
        for k, v in counts.items():
            grand[k] += v
        days = len({r.report_date for r in refs})
        print(
            f"job {job.get('name'):<34} {job['reportTypeId']:<32} reports={len(refs):>4} "
            f"days={days:>3} archived={counts['archived']:>3} exists={counts['exists']:>3} "
            f"would={counts['would']:>3} failed={counts['failed']:>2}"
        )

    print(
        f"\nTOTAL reports={grand['reports']} archived_now={grand['archived']} "
        f"already_present={grand['exists']} would_archive={grand['would']} "
        f"failed={grand['failed']} csv_bytes_downloaded={grand['bytes']:,}"
    )
    for f in failures:
        print(f"  FAILED {f}", file=sys.stderr)

    ok = True
    if grand["reports"] == 0:
        print("RESULT: FAIL  jobs exist but zero reports were listed", file=sys.stderr)
        ok = False
    if grand["failed"]:
        ok = False

    if not args.dry_run and grand["reports"]:
        after = {b.name for b in gcs.list_blobs(bucket_name)}
        missing = [object_name(r) for r in all_refs if object_name(r) not in after]
        print(f"objects in bucket after run: {len(after)}; listed reports: {len(all_refs)}; "
              f"listed reports missing from bucket: {len(missing)}")
        for m in missing[:20]:
            print(f"  MISSING {m}", file=sys.stderr)
        if missing:
            ok = False

    if args.verify or args.verify_report:
        candidates = [r for r in all_refs if object_name(r) in (existing | {object_name(x) for x in all_refs})]
        if args.verify_report:
            candidates = [r for r in candidates if r.report_id == args.verify_report]
        if args.dry_run or not candidates:
            print("VERIFY: nothing to verify (dry run, or no archived report matches)", file=sys.stderr)
            ok = ok and not args.verify_report
        else:
            # Deterministic choice: the newest report by create_time, so a re-run verifies
            # the most recent thing the archive stored.
            ref = max(candidates, key=lambda r: (r.create_time, r.report_id))
            ok = verify_one(bucket, yt, ref) and ok

    print(f"RESULT: {'OK' if ok else 'FAIL'}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
