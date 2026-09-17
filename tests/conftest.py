"""Shared fixtures: keep the process-global storage config isolated per test."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from fina_risk.storage import clear_storage_override, reload_storage_config


def pytest_sessionstart(session: object) -> None:
    """Generate ignored deterministic benchmark files for a clean checkout."""
    del session
    root = Path(__file__).resolve().parents[1]
    benchmark = root / "benchmark"
    if (benchmark / "instruments.json").exists() and (benchmark / "market.json").exists():
        return
    runpy.run_path(str(root / "scripts/generate_benchmark.py"), run_name="__main__")


@pytest.fixture(autouse=True)
def _reset_storage_config():
    """Clear runtime overrides and re-read env before and after every test."""
    clear_storage_override()
    reload_storage_config()
    yield
    clear_storage_override()
    reload_storage_config()
