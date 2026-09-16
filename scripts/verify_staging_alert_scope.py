#!/usr/bin/env python3
"""Assert staging alert policies have ERROR severity and exact staging resource filters."""

import json
import subprocess
import sys


PROJECT = subprocess.run(
    ["gcloud", "config", "get-value", "project"], text=True, capture_output=True, check=True
).stdout.strip()
result = subprocess.run(
    ["gcloud", "beta", "monitoring", "policies", "list", f"--project={PROJECT}", "--format=json"],
    text=True,
    capture_output=True,
    check=True,
)
policies = {item["displayName"]: item for item in json.loads(result.stdout)}


def policy_filter(policy):
    return "\n".join(
        condition.get("conditionMatchedLog", {}).get("filter", "")
        for condition in policy.get("conditions", [])
    )

expected = {
    "youtube-analytics-failure-staging": "youtube-bigquery-pipeline-staging",
    "youtube-reporting-failure-staging": "youtube-reporting-ingest-staging",
    "youtube-reporting-stale-staging": "youtube-reporting-ingest-staging",
    "youtube-refresh-failure-staging": "youtube-analytics-refresh-staging",
}
errors = []
for name, service in expected.items():
    policy = policies.get(name)
    if not policy:
        errors.append(f"missing {name}")
        continue
    filter_text = policy_filter(policy)
    if policy.get("severity") != "ERROR":
        errors.append(f"{name} severity is not ERROR")
    if f'resource.labels.service_name="{service}"' not in filter_text:
        errors.append(f"{name} does not match exact service {service}")
    production = service.removesuffix("-staging")
    if f'resource.labels.service_name="{production}"' in filter_text:
        errors.append(f"{name} can match production service {production}")

scheduler_name = "youtube-scheduler-failure-staging"
scheduler = policies.get(scheduler_name)
if not scheduler:
    errors.append(f"missing {scheduler_name}")
else:
    filter_text = policy_filter(scheduler)
    if scheduler.get("severity") != "ERROR":
        errors.append(f"{scheduler_name} severity is not ERROR")
    for job in (
        "youtube-daily-snapshot-staging",
        "youtube-reporting-daily-staging",
        "youtube-analytics-refresh-weekly-staging",
    ):
        if f'resource.labels.job_id="{job}"' not in filter_text:
            errors.append(f"{scheduler_name} lacks {job}")
    for production in (
        "youtube-daily-snapshot",
        "youtube-reporting-daily",
        "youtube-analytics-refresh-weekly",
    ):
        if f'resource.labels.job_id="{production}"' in filter_text:
            errors.append(f"{scheduler_name} can match production job {production}")

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print("STAGING ALERT SCOPE: PASS")
