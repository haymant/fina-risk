"""Shared fixtures: keep the process-global storage config isolated per test."""

from __future__ import annotations

import pytest

from fina_risk.storage import clear_storage_override, reload_storage_config


@pytest.fixture(autouse=True)
def _reset_storage_config():
    """Clear runtime overrides and re-read env before and after every test."""
    clear_storage_override()
    reload_storage_config()
    yield
    clear_storage_override()
    reload_storage_config()
