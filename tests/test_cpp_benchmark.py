from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "cpp" / "build" / "fina-risk-cpp-benchmark"


@pytest.mark.skipif(not BINARY.exists(), reason="build cpp target first")
def test_cpp_benchmark_emits_python_compatible_metrics() -> None:
    result = subprocess.run(
        [str(BINARY), str(ROOT / "benchmark/instruments.json"), str(ROOT / "benchmark/market.json"), "8"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    benchmark = payload["benchmark"]
    assert benchmark["backend"] == "cpp_reference_optional_quantlib_xad"
    assert benchmark["instruments"] == 2000
    assert benchmark["underlyings"] == 1200
    assert benchmark["paths"] == 8
    assert benchmark["instruments_per_second"] > 0
    assert benchmark["peak_path_cube_bytes"] > 0
