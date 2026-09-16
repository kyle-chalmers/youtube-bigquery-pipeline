"""Manual table writers use the same dataset-scoped leases as scheduled writers."""

from pathlib import Path

import run_lease


ROOT = Path(__file__).resolve().parent.parent


def test_manual_dry_run_does_not_build_a_lease(monkeypatch):
    monkeypatch.setattr(
        run_lease,
        "build_run_lease",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not lease")),
    )
    with run_lease.manual_writer_lease(
        project_id="p",
        dataset="d",
        domain="reporting-writer",
        entrypoint="backfill_reporting",
        dry_run=True,
    ):
        pass


def test_manual_writer_uses_explicit_bucket_and_owner(monkeypatch):
    calls = []

    class Lease:
        def __enter__(self):
            calls.append("enter")

        def __exit__(self, *args):
            calls.append("exit")

    monkeypatch.setenv("PIPELINE_LOCK_BUCKET", "stage-locks")
    monkeypatch.setattr(run_lease, "build_run_lease", lambda **kwargs: calls.append(kwargs) or Lease())
    with run_lease.manual_writer_lease(
        project_id="p",
        dataset="d",
        domain="analytics-writer",
        entrypoint="backfill_analytics",
        run_id="manual-1",
    ):
        pass

    assert calls == [
        {
            "project_id": "p",
            "bucket_name": "stage-locks",
            "dataset": "d",
            "domain": "analytics-writer",
            "run_id": "manual-1",
            "entrypoint": "backfill_analytics",
            "ttl_seconds": 2100,
        },
        "enter",
        "exit",
    ]


def test_every_manual_writer_declares_its_domain_and_lease():
    expected = {
        "backfill_reporting.py": "reporting-writer",
        "backfill_analytics.py": "analytics-writer",
        "refresh_analytics.py": "analytics-writer",
        "repair_reporting_duplicates.py": "reporting-writer",
        "snapshot_reporting_tables.py": "reporting-writer",
    }
    for filename, domain in expected.items():
        source = (ROOT / "setup" / filename).read_text()
        assert "manual_writer_lease" in source
        assert f'domain="{domain}"' in source


def test_reporting_dry_run_is_forwarded_to_lease_helper():
    source = (ROOT / "setup" / "backfill_reporting.py").read_text()
    assert "dry_run=args.dry_run" in source
