"""The BigQuery write fence is preseeded and setup applies it to every dataset."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_mutex_ddl_seeds_exactly_the_two_static_domains_and_asserts_cardinality():
    sql = (ROOT / "sql" / "pipeline_write_mutex.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS `${BQ_DATASET}.pipeline_write_mutex_analytics`" in sql
    assert "CREATE TABLE IF NOT EXISTS `${BQ_DATASET}.pipeline_write_mutex_reporting`" in sql
    assert "CREATE OR REPLACE VIEW `${BQ_DATASET}.pipeline_write_mutex`" in sql
    assert "'analytics'" in sql and "'reporting'" in sql
    assert "ASSERT" in sql
    assert "COUNTIF(mutex_name = 'analytics') = 1" in sql
    assert "COUNTIF(mutex_name = 'reporting') = 1" in sql


def test_bigquery_setup_applies_mutex_after_base_and_reporting_tables():
    script = (ROOT / "setup" / "2_create_bigquery.sh").read_text()
    base = script.index("sql/create_tables.sql")
    reporting = script.index("sql/reporting_tables.sql")
    mutex = script.index("sql/pipeline_write_mutex.sql")
    assert base < reporting < mutex
