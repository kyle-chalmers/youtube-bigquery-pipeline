"""Parse a Reporting API CSV body into typed rows, by header name.

Column order in these CSVs is not stable (observed: channel_basic_a3 puts `views` before
`engaged_views`; the docs list them the other way round), so every value is looked up by
its header name. A column the spec does not know, or a spec column the CSV lacks, is
schema drift and raises rather than silently producing a table with a hole in it.

Type rules: `date` YYYYMMDD becomes an ISO date string for BigQuery's DATE column;
dimensions are strings with "" mapped to NULL; metrics are int or float per METRIC_TYPES
with "" mapped to NULL and any non-numeric text raising. No silent coercion.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

from report_specs import DIMENSIONS, METRIC_TYPES, ReportSpec


class SchemaDriftError(ValueError):
    """The CSV header does not match the report spec."""


def _parse_date(value: str) -> str:
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"bad report date {value!r}; expected YYYYMMDD")
    return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()


def _coerce_metric(name: str, value: str) -> int | float | None:
    if value == "":
        return None
    kind = METRIC_TYPES[name]
    try:
        if kind == "INT64":
            # Some counters arrive as "12.0" in older report versions; accept an exact integer float.
            if "." in value:
                f = float(value)
                if not f.is_integer():
                    raise ValueError
                return int(f)
            return int(value)
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError
        return f
    except ValueError:
        raise ValueError(f"metric {name}={value!r} is not a valid {kind}") from None


def parse_report(raw: bytes, spec: ReportSpec) -> list[dict[str, Any]]:
    """Turn a CSV body into rows keyed by table column names (date -> report_date)."""
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise SchemaDriftError(f"{spec.report_type}: empty body, not even a header") from None
    header = [h.strip() for h in header]

    got = set(header)
    unknown = got - spec.csv_columns
    missing = spec.csv_columns - got
    if unknown or missing:
        raise SchemaDriftError(
            f"{spec.report_type}: header drift; unknown={sorted(unknown)} missing={sorted(missing)}"
        )
    if len(header) != len(got):
        raise SchemaDriftError(f"{spec.report_type}: duplicate header columns")

    idx = {name: i for i, name in enumerate(header)}
    rows: list[dict[str, Any]] = []
    for line_no, values in enumerate(reader, start=2):
        if not values or (len(values) == 1 and values[0] == ""):
            continue  # trailing blank line
        if len(values) != len(header):
            raise SchemaDriftError(
                f"{spec.report_type}: line {line_no} has {len(values)} fields, header has {len(header)}"
            )
        row: dict[str, Any] = {}
        for name in spec.dimensions:
            v = values[idx[name]]
            if name == "date":
                row["report_date"] = _parse_date(v)
            else:
                row[name] = v if v != "" else None
        for name in spec.metrics:
            row[name] = _coerce_metric(name, values[idx[name]])
        rows.append(row)
    return rows


def dimension_columns_in_header(header: list[str]) -> list[str]:
    """Utility for diagnostics: which header columns are dimensions."""
    return [h for h in header if h in DIMENSIONS]
