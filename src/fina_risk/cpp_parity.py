"""C++ parity lane for the fina-risk benchmark tool.

The MCP tool layer exposes a `backend` selector so a caller chooses which parity
executes the shared-corpus benchmark: `"python"` (NumPy reference kernel) or
`"cpp"`. Both parities must produce the same pricing results because they
consume the exact same float32 terminal cube, instrument corpus, strikes, spots,
seeds, and bump conventions.

Execution strategy (honest, no mislabeling):

- `native`: when the compiled ``fina_risk_cpp`` pybind module is importable
  (local/CI Linux build with CMake + OpenMP), the C++ ``run_cpp_parity`` kernel
  runs. The module owns no MCP transport; it is driven through this adaptor.
- `python_mirror`: when the native module is unavailable (Vercel Python-only
  runtime), an exact pure-Python mirror of the C++ math runs. The mirror
  reproduces the parity kernel's per-instrument central-bump PV/delta/gamma and
  Taylor P&L loop against the identical cube, so results equal the C++ lane
  within float64 accumulation order.

AAD is intentionally disabled for the C++ parity lane: the C++ fallback math is
finite-difference / pathwise by design. The report records
``aad_engine: "disabled"`` and method ``CRN_BUMP_REVALUE_WITH_PATHWISE_DELTA``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import numpy as np

try:  # pragma: no cover - importable only when the native module is built
    import fina_risk_cpp  # type: ignore[import-not-found]

    def _native_module() -> Any | None:
        return fina_risk_cpp

except ImportError:  # pragma: no cover
    def _native_module() -> Any | None:  # type: ignore[no-redef]
        return None


def collect_cpu_info() -> dict[str, Any]:
    from .system_info import collect_cpu_info as _collect  # deferred import

    return _collect()


def _native_cpp_parity(
    corpus: dict[str, Any],
    *,
    instruments: int,
    underlyings: int,
    factors: int,
    seed: int,
    bump: float,
) -> dict[str, Any]:
    spots = corpus["spots"]
    node = _native_module()
    if node is None:
        return {"available": False}
    terminal = np.ascontiguousarray(corpus["terminal"], dtype=np.float32)
    market = {"underlyings": [{"id": f"EQ{i:04d} US", "spot": float(spots[i])} for i in range(underlyings)]}
    records: list[dict[str, Any]] = []
    for index in range(instruments):
        key = index % corpus["unique_baskets"]
        idx = corpus["basket_idx"][key]
        records.append(
            {
                "instrumentId": f"ELI-BENCH-{index:05d}",
                "underlyings": [f"EQ{int(i):04d} US" for i in idx],
                "legs": [
                    {
                        "leg_id": 1,
                        "leg_type": "intrinsic_option",
                        "leg_name": "PUT",
                        "multiplier": -1,
                        "payoff": {"basket": "worst_of", "strike": float(corpus["strikes"][key])},
                    }
                ],
            }
        )
    payload = json.loads(
        node.run_cpp_parity(
            json.dumps({"instruments": records}),
            json.dumps(market),
            terminal,
            seed,
            bump,
        )
    )
    payload["available"] = True
    payload["backend_engine"] = "cpp_native"
    payload["factors"] = factors
    return payload


def _mirror_cpp_parity(
    corpus: dict[str, Any],
    *,
    instruments: int,
    bump: float,
) -> dict[str, Any]:
    """Exact pure-Python reproduction of ``run_cpp_parity``'s inner loop."""
    terminal = corpus["terminal"]
    spots = corpus["spots"]
    basket_idx = corpus["basket_idx"]
    strikes = corpus["strikes"]
    unique_baskets = corpus["unique_baskets"]
    started = time.perf_counter()
    pv_checksum = 0.0
    delta_checksum = 0.0
    delta_dollar_checksum = 0.0
    gamma_checksum = 0.0
    forecast_checksum = 0.0
    actual_checksum = 0.0
    for index in range(instruments):
        key = index % unique_baskets
        idx = basket_idx[key]
        spot_vals = spots[idx].astype(np.float64)
        struck = float(strikes[key])
        ratios = terminal[:, idx].astype(np.float64) / spot_vals[None, :]
        worst = ratios.min(axis=1)
        put = np.maximum(struck - worst, 0.0).mean()
        base_pv = 1.0 - put
        for k in range(idx.size):
            up = ratios.copy()
            down = ratios.copy()
            up[:, k] *= 1.0 + bump
            down[:, k] *= 1.0 - bump
            up_pv = float(np.maximum(struck - up.min(axis=1), 0.0).mean())
            down_pv = float(np.maximum(struck - down.min(axis=1), 0.0).mean())
            delta = (up_pv - down_pv) / (2.0 * bump * spot_vals[k])
            gamma = (up_pv - 2.0 * put + down_pv) / (bump * spot_vals[k]) ** 2
            delta_checksum += delta
            delta_dollar_checksum += delta * spot_vals[k]
            gamma_checksum += gamma
            forecast_checksum += delta * spot_vals[k] * bump + 0.5 * gamma * (spot_vals[k] * bump) ** 2
        actual = float(np.maximum(struck - worst * (1.0 + bump), 0.0).mean()) - put
        pv_checksum += base_pv
        actual_checksum += actual
    elapsed = time.perf_counter() - started
    return {
        "available": True,
        "instruments": instruments,
        "underlyings": int(spots.size),
        "paths": int(terminal.shape[0]),
        "pv_checksum": pv_checksum,
        "delta_checksum": delta_checksum,
        "delta_dollar_checksum": delta_dollar_checksum,
        "gamma_checksum": gamma_checksum,
        "taylor_forecast_checksum": forecast_checksum,
        "taylor_actual_checksum": actual_checksum,
        "taylor_unexplained_checksum": actual_checksum - forecast_checksum,
        "elapsed_seconds": elapsed,
        "instruments_per_second": instruments / max(elapsed, 1e-12),
        "backend_engine": "cpp_parity_python_mirror",
    }


def run_cpp_parity_benchmark(
    *,
    instruments: int = 100000,
    underlyings: int = 1200,
    paths: int = 1000,
    factors: int = 12,
    seed: int = 20260909,
    execution: dict[str, Any] | None = None,
    bump: float = 0.01,
) -> dict[str, Any]:
    """Execute the C++ parity lane on the shared corpus with AAD disabled."""
    from .benchmarking import build_benchmark_corpus

    execution = execution or {}
    structure_cache = bool(execution.get("structure_cache", True))
    unique_structure_count = max(
        1, min(int(execution.get("unique_structure_count", min(instruments, 1000))), instruments)
    )
    corpus = build_benchmark_corpus(
        instruments=instruments,
        underlyings=underlyings,
        paths=paths,
        factors=factors,
        seed=seed,
        unique_structure_count=unique_structure_count,
        structure_cache=structure_cache,
    )
    native_t0 = time.perf_counter()
    native = _native_cpp_parity(
        corpus,
        instruments=instruments,
        underlyings=underlyings,
        factors=factors,
        seed=seed,
        bump=bump,
    )
    native_attempt_seconds = time.perf_counter() - native_t0
    if native.get("available"):
        payload = native
    else:
        payload = _mirror_cpp_parity(corpus, instruments=instruments, bump=bump)
    payload["factors"] = factors
    payload["seed"] = seed
    payload["bump"] = bump
    payload["backend"] = "cpp"
    payload["backend_engine"] = payload["backend_engine"]
    payload["native_attempt_seconds"] = native_attempt_seconds
    payload["structure_reuse"] = instruments > corpus["unique_baskets"]
    payload["unique_payoff_baskets"] = corpus["unique_baskets"]
    payload["pricing_kernel"] = "cpp_run_cpp_parity.v1"
    payload["aad_engine"] = "disabled"
    payload["aad_reason"] = "C++ parity lane is finite-difference/pathwise; XAD AAD not used"
    payload["method"] = "CRN_BUMP_REVALUE_WITH_PATHWISE_DELTA"
    payload["greek_scope"] = ["delta", "gamma"]
    payload["taylor_scope"] = "taylor2"
    payload["elapsed_seconds"] = payload["elapsed_seconds"] + native_attempt_seconds
    payload["cpu"] = collect_cpu_info()
    return {"benchmark": payload, "instrument_results": []}


if __name__ == "__main__":
    print(json.dumps(run_cpp_parity_benchmark(instruments=10, underlyings=60, paths=100), indent=2))