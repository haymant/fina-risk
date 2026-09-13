"""Shared store-config resolution: same env vars/overrides as fina-olap."""

from __future__ import annotations

import pytest

from fina_risk.olap import resolve_dataset_source, resolve_s3_uri
from fina_risk.storage import (
    get_storage_config,
    reload_storage_config,
    set_storage_override,
    storage_status,
)


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FINA_OLAP_STORE",
        "FINA_OLAP_PARQUET_ROOT",
        "OLAP_PARQUET_ROOT",
        "DATA_DIR",
        "FINA_RISK_OLAP_ROOT",
        "FINA_OLAP_BUCKET",
        "S3_BUCKET_NAME",
        "AWS_BUCKET",
        "GCS_BUCKET_NAME",
        "FINA_OLAP_PATH",
        "S3_PATH_ENV",
        "S3_PATH_TEMPLATE",
        "FINA_OLAP_PARTITION_GLOB",
        "FINA_OLAP_HIVE_PARTITIONING",
        "S3_API_KEY",
        "S3_API_SECRET",
        "S3_BUCKET_NAME",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_BUCKET",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_local_root_env_and_default_glob(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("FINA_OLAP_STORE", "local")
    monkeypatch.setenv("FINA_OLAP_PARQUET_ROOT", str(tmp_path))
    reload_storage_config()

    assert get_storage_config().store == "local"
    source, hive = resolve_dataset_source("risk_wide")
    assert source == f"{tmp_path.as_posix()}/risk_wide*.parquet"
    assert hive is False  # local defaults to no hive


def test_legacy_fina_risk_root_alias(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("FINA_RISK_OLAP_ROOT", str(tmp_path))
    reload_storage_config()
    assert get_storage_config().local_root == tmp_path.as_posix()


def test_partition_glob_and_hive(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("FINA_OLAP_STORE", "local")
    monkeypatch.setenv("FINA_OLAP_PARQUET_ROOT", str(tmp_path))
    monkeypatch.setenv("FINA_OLAP_PARTITION_GLOB", "{tableName}/*.parquet")
    monkeypatch.setenv("FINA_OLAP_HIVE_PARTITIONING", "1")
    reload_storage_config()

    source, hive = resolve_dataset_source("risk_wide")
    assert source.endswith("risk_wide/*.parquet")
    assert hive is True


def test_s3_bucket_source_and_runtime_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("FINA_OLAP_STORE", "s3")
    monkeypatch.setenv("S3_BUCKET_NAME", "s3://fina-olap-test")
    monkeypatch.setenv("FINA_OLAP_PATH", "warehouse")
    monkeypatch.setenv("S3_API_KEY", "key")
    monkeypatch.setenv("S3_API_SECRET", "secret")
    reload_storage_config()

    source, hive = resolve_dataset_source("risk_wide")
    assert source == "s3://fina-olap-test/warehouse/risk_wide*.parquet"
    assert hive is True

    # runtime override (as the MCP store_configure tool applies) wins over env
    set_storage_override(partition_glob="risk_wide/region=*/*.parquet")
    assert storage_status()["overrides"]["partition_glob"] == "risk_wide/region=*/*.parquet"
    assert resolve_dataset_source("risk_wide")[0].endswith("risk_wide/region=*/*.parquet")


def test_resolve_s3_uri_uses_shared_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("FINA_OLAP_STORE", "s3")
    monkeypatch.setenv("S3_BUCKET_NAME", "fina-riskcube")
    monkeypatch.setenv("S3_ENDPOINT", "storage.googleapis.com")
    monkeypatch.setenv("S3_API_KEY", "key")
    monkeypatch.setenv("S3_API_SECRET", "secret")
    reload_storage_config()

    uri = resolve_s3_uri({"version": "v1", "sliceName": "2026-09-12", "tableName": "risk_wide"})
    assert uri == "s3://fina-riskcube/v1/2026-09-12/risk_wide*.parquet"
