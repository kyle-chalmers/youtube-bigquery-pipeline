#!/usr/bin/env python3
"""Create YouTube Reporting API jobs for every channel report type that lacks one.

A job costs nothing and is reversible (jobs.delete), but it only backfills the 30 days
before its creation, so the only irreversible part of this script is the clock it
starts. Run without flags to see what would be created; pass --create to do it.

    python3 setup/7_create_reporting_jobs.py                       # dry run, prints the plan
    python3 setup/7_create_reporting_jobs.py --create              # creates every missing type
    python3 setup/7_create_reporting_jobs.py --create --only A B   # only these report type ids

Job names are kc_<report_type_id> so they sort next to each other in listings. An HTTP
409 means a job for that type already exists and is reported, not treated as failure.
An empty report-type list is treated as failure: it means the credential cannot see the
channel, not that there is nothing to create.

Requires GCP_PROJECT (or an active gcloud project) and the four OAuth secrets in Secret
Manager. Writes nothing to BigQuery.
"""

import argparse
import sys

from googleapiclient.errors import HttpError

import _bootstrap  # noqa: F401  (adds cloud_function/ to sys.path)

# isort: split   (everything below needs _bootstrap to have run first)
from oauth_credentials import load_oauth_credentials
from youtube_reporting_api import YouTubeReportingClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--create", action="store_true", help="actually create jobs")
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        help="restrict to these report type ids (default: every missing type)",
    )
    args = parser.parse_args()

    project = _bootstrap.resolve_project()
    client = YouTubeReportingClient(load_oauth_credentials(project))

    types = {t["id"]: t.get("name", "") for t in client.list_report_types()}
    if not types:
        print("No report types listed. The credential cannot see this channel's reports "
              "(scope or account problem). Nothing created.", file=sys.stderr)
        return 1
    jobs = client.list_jobs()
    have = {j["reportTypeId"]: j for j in jobs}

    print(f"{len(types)} report types available, {len(jobs)} jobs exist\n")
    print(f"{'report type':<40} {'status':<10} job")
    for rtid in sorted(types):
        j = have.get(rtid)
        status = "exists" if j else "MISSING"
        detail = f"{j['id']}  created {j.get('createTime', '')[:10]}  name={j.get('name')}" if j else ""
        print(f"{rtid:<40} {status:<10} {detail}")

    missing = [t for t in sorted(types) if t not in have]
    if args.only is not None:
        unknown = set(args.only) - set(types)
        if unknown:
            sys.exit(f"Unknown report type ids: {sorted(unknown)}")
        missing = [t for t in missing if t in args.only]

    print(f"\n{len(missing)} job(s) to create: {', '.join(missing) or 'none'}")
    if not args.create:
        print("Dry run. Re-run with --create to create them.")
        return 0

    created, existed, failed = [], [], []
    for rtid in missing:
        try:
            job = client.create_job(name=f"kc_{rtid}", report_type_id=rtid)
            created.append((rtid, job["id"], job.get("createTime", "")))
            print(f"CREATED {rtid:<36} jobId={job['id']}  createTime={job.get('createTime')}")
        except HttpError as e:
            if e.resp.status == 409:
                existed.append(rtid)
                print(f"EXISTS  {rtid:<36} (HTTP 409)")
            else:
                failed.append((rtid, str(e)))
                print(f"FAILED  {rtid:<36} {e}")

    print(f"\ncreated={len(created)} already_existed={len(existed)} failed={len(failed)}")
    if created:
        print("\nRecord these in .internal/OWNER_CONFIG.md (job table):")
        for rtid, job_id, ts in created:
            print(f"| `kc_{rtid}` | `{rtid}` | `{job_id}` | {ts[:10]} |")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
