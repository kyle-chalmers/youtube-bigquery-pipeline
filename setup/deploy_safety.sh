#!/usr/bin/env bash

# Shared fail-closed validation for every writer deployment. Source this file, then call
# validate_writer_deploy before the first gcloud mutation.

duration_seconds() {
    local value="$1"
    value="${value%s}"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        echo "Refusing: timeout and lease TTL values must be whole seconds." >&2
        return 1
    fi
    printf '%s\n' "$value"
}

validate_writer_deploy() {
    local function_name="$1" dataset="$2" bucket="$3" project="$4"
    local timeout_raw="$5" ttl_raw="$6" work_budget_raw="${7:-}"
    local prod_bucket="${project}-youtube-pipeline-locks-prod"
    local staging_bucket="${project}-youtube-pipeline-locks-staging"
    local timeout_seconds ttl_seconds work_budget_seconds
    local lease_release_margin_seconds=300

    if [[ "$dataset" == "youtube_analytics" && "$bucket" != "$prod_bucket" ]]; then
        echo "Refusing: the production dataset requires the production lock bucket." >&2
        return 1
    fi
    if [[ "$function_name" == *-staging || "$dataset" == *_staging ]]; then
        if [[ "$bucket" != "$staging_bucket" ]]; then
            echo "Refusing: a staging writer requires the staging lock bucket." >&2
            return 1
        fi
    elif [[ "$bucket" == "$staging_bucket" ]]; then
        echo "Refusing: a non-staging writer cannot use the staging lock bucket." >&2
        return 1
    fi

    timeout_seconds="$(duration_seconds "$timeout_raw")" || return 1
    ttl_seconds="$(duration_seconds "$ttl_raw")" || return 1
    if (( ttl_seconds < timeout_seconds + lease_release_margin_seconds )); then
        echo "Refusing: the writer lease TTL must exceed the function timeout by at least ${lease_release_margin_seconds} seconds." >&2
        return 1
    fi

    if [[ -n "$work_budget_raw" ]]; then
        work_budget_seconds="$(duration_seconds "$work_budget_raw")" || return 1
        if (( work_budget_seconds >= timeout_seconds )); then
            echo "Refusing: the Reporting work budget must be shorter than the function timeout." >&2
            return 1
        fi
    fi
}
