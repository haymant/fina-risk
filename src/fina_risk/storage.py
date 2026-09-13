"""Env-driven storage backend + Parquet partition configuration.

Mirrors ``fina_olap.storage`` so fina-risk and fina-olap share the **same env
vars** and runtime overrides, letting a risk-generation run write to the exact
location an OLAP session reads:

- ``FINA_OLAP_STORE``            — ``local`` | ``s3`` | ``gcs`` | ``auto`` (default).
- ``FINA_OLAP_PARQUET_ROOT``     — local Parquet root (aliases: ``OLAP_PARQUET_ROOT``,
                                   ``DATA_DIR``, and fina-risk's legacy ``FINA_RISK_OLAP_ROOT``).
- ``FINA_OLAP_BUCKET``           — object-store bucket (aliases: ``S3_BUCKET_NAME``,
                                   ``AWS_BUCKET``; ``GCS_BUCKET_NAME`` for gcs).
- ``FINA_OLAP_PATH``             — default prefix under the bucket (alias ``S3_PATH_ENV``).
- ``S3_PATH_TEMPLATE``           — explicit ``{bucket}/{s3Path}/{version}/{slice}/{tableName}*`` layout.
- ``FINA_OLAP_PARTITION_GLOB``   — glob describing the on-store layout, e.g.
                                   ``{tableName}/*.parquet``. Default ``{tableName}*.parquet``.
- ``FINA_OLAP_HIVE_PARTITIONING``— ``1/0/true/false``; defaults on for s3/gcs, off for local.

Runtime overrides set through :func:`set_storage_override` (exposed over MCP as
``store_configure``) sit on top of the env base and are process-local.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

ALLOWED_STORES = ("local", "s3", "gcs", "auto")

#: fina-risk's historical local root, kept as the fallback when nothing else is set.
DEFAULT_LOCAL_ROOT = "/tmp/fina-risk-olap"

_OVERRIDE_FIELDS = ("store", "root", "bucket", "default_path", "path_template", "partition_glob", "hive_partitioning")

#: Runtime (process-local) config overrides applied on top of the env base.
_OVERRIDES: dict[str, Any] = {}

_ALIASES: dict[str, tuple[str, ...]] = {
    "root": ("FINA_OLAP_PARQUET_ROOT", "OLAP_PARQUET_ROOT", "DATA_DIR", "FINA_RISK_OLAP_ROOT"),
    "bucket": ("FINA_OLAP_BUCKET", "S3_BUCKET_NAME", "AWS_BUCKET"),
    "path": ("FINA_OLAP_PATH", "S3_PATH_ENV"),
}


def _first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value {value!r} for FINA_OLAP_HIVE_PARTITIONING")


def _normalize_store(value: str | None) -> str:
    store = (value or "auto").strip().lower()
    if store not in ALLOWED_STORES:
        raise ValueError(f"invalid FINA_OLAP_STORE {value!r} (expected one of {', '.join(ALLOWED_STORES)})")
    return store


@dataclass(frozen=True)
class StorageConfig:
    """Resolved, env-driven storage + partition settings (no secrets)."""

    store: str = "auto"
    root: str | None = None
    bucket: str | None = None
    default_path: str = ""
    path_template: str | None = None
    partition_glob: str | None = None
    hive_partitioning: bool | None = None

    @property
    def effective_bucket(self) -> str | None:
        if self.bucket:
            return self.bucket.rstrip("/")
        if self.store == "gcs":
            bucket = _first("GCS_BUCKET_NAME")
            return bucket.rstrip("/") if bucket else None
        return None

    @property
    def scheme(self) -> str:
        return "gs://" if self.store == "gcs" else "s3://"

    def is_object_store(self) -> bool:
        return self.store in ("s3", "gcs")

    @property
    def auto_hive(self) -> bool:
        return self.is_object_store()

    def glob_for(self, table: str) -> str:
        pattern = self.partition_glob or "{tableName}*.parquet"
        return pattern.replace("{table}", table).replace("{tableName}", table)

    @property
    def local_root(self) -> str:
        """Local root for local/auto fallback (never None)."""
        return (self.root or DEFAULT_LOCAL_ROOT).rstrip("/")


def get_storage_config() -> StorageConfig:
    config = _build_storage_config()
    changes = {key: value for key, value in _OVERRIDES.items() if hasattr(config, key)}
    if not changes:
        return config
    return dataclasses.replace(config, **changes)


@lru_cache(maxsize=1)
def _build_storage_config() -> StorageConfig:
    root = _first(*_ALIASES["root"])
    bucket = _first(*_ALIASES["bucket"]) or _first("GCS_BUCKET_NAME")
    return StorageConfig(
        store=_normalize_store(os.getenv("FINA_OLAP_STORE")),
        root=root,
        bucket=bucket,
        default_path=(_first(*_ALIASES["path"]) or "").strip("/"),
        path_template=os.getenv("S3_PATH_TEMPLATE"),
        partition_glob=os.getenv("FINA_OLAP_PARTITION_GLOB"),
        hive_partitioning=_parse_bool(os.getenv("FINA_OLAP_HIVE_PARTITIONING")),
    )


def reload_storage_config() -> StorageConfig:
    """Drop the memoized config and rebuild from the current environment."""
    _build_storage_config.cache_clear()
    return get_storage_config()


def set_storage_override(**kwargs: Any) -> StorageConfig:
    """Apply runtime overrides on top of the env-backed config (process-local)."""
    normalized: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key not in _OVERRIDE_FIELDS:
            raise ValueError(f"unknown storage config field {key!r} (expected one of {', '.join(_OVERRIDE_FIELDS)})")
        if key == "store":
            normalized[key] = _normalize_store(str(value))
        elif key == "hive_partitioning":
            text = value if isinstance(value, str) else ("true" if value else "false")
            parsed = _parse_bool(text)
            assert parsed is not None
            normalized[key] = parsed
        else:
            normalized[key] = value if value is None or isinstance(value, str) else str(value)
    _OVERRIDES.update(normalized)
    return get_storage_config()


def clear_storage_override() -> StorageConfig:
    _OVERRIDES.clear()
    return get_storage_config()


def storage_overrides() -> dict[str, Any]:
    return dict(_OVERRIDES)


def storage_status() -> dict[str, Any]:
    """Non-secret diagnostics for health endpoints / MCP status."""
    cfg = get_storage_config()
    return {
        "store": cfg.store,
        "root": cfg.root,
        "bucket": cfg.effective_bucket,
        "path": cfg.default_path,
        "path_template_set": bool(cfg.path_template),
        "partition_glob": cfg.partition_glob,
        "hive_partitioning": cfg.hive_partitioning if cfg.hive_partitioning is not None else cfg.auto_hive,
        "hive_partitioning_explicit": cfg.hive_partitioning is not None,
        "overrides": storage_overrides(),
    }


__all__ = [
    "ALLOWED_STORES",
    "DEFAULT_LOCAL_ROOT",
    "StorageConfig",
    "clear_storage_override",
    "get_storage_config",
    "reload_storage_config",
    "set_storage_override",
    "storage_overrides",
    "storage_status",
]
