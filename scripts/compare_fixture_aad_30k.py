from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from fina_risk.aad import aad_fixed_branch_market_sensitivities
from fina_risk.pricing import (
    _simulate,
    bump_result,
    common_from_job,
    market_from_legacy,
    price_fixture,
    price_terminal_legs,
)


def main() -> None:
    raw = json.loads(Path("skills/fina-risk/refs/termsheet1.md.json").read_text())
    job = raw["Chunk"]["Jobs"][0] if "Chunk" in raw else raw
    common = common_from_job(job)
    paths = 30_000
    seed = 1729
    base = price_fixture(common, paths=paths, seed=seed)
    crn = bump_result(common, paths=paths, seed=seed)
    aad_deltas = base["aad"]["deltas"]
    pathwise = [x["value"] for x in base["sensitivities"] if x["risk_factor_id"].startswith("EQ:")]
    fd = [x["value"] for x in crn["fd_sensitivities"] if x["risk_factor_id"].startswith("EQ:")]

    market = market_from_legacy(common, paths=paths, seed=seed)
    terminal1, terminal2, _ = _simulate(market, int(common["dealData"]["expiryDate"]), steps=194)
    terminal = np.column_stack((terminal1, terminal2))
    refs = market.reference_spots
    strike = float(common["dealData"].get("knockInStar", {}).get("strikeKI2", 0.78))
    df = math.exp(-market.rate * base["explainability"]["time_to_expiry"])
    multipliers = terminal / market.quoted_spots[None, :]
    log_returns = np.log(np.maximum(multipliers, 1e-12))
    skew_basis = np.zeros_like(log_returns)
    aad_market = aad_fixed_branch_market_sensitivities(
        market.quoted_spots,
        refs,
        multipliers,
        strike,
        df,
        log_returns=log_returns,
        skew_basis=skew_basis,
    )

    def pv(terminal_spots: np.ndarray, discount: float = df) -> float:
        return float(
            price_terminal_legs(terminal_spots, market.quoted_spots, refs, strike, discount, 0.0)["put_option_price"]
        )

    vol_h = 0.01
    vol_up = market.quoted_spots[None, :] * np.exp(log_returns * (1.0 + vol_h))
    vol_down = market.quoted_spots[None, :] * np.exp(log_returns * (1.0 - vol_h))
    crn_vega = (pv(vol_up) - pv(vol_down)) / (2.0 * vol_h)
    rate_h = 0.0001
    crn_irpv01 = (pv(terminal, df - rate_h) - pv(terminal, df + rate_h)) / 2.0
    crn_skew = 0.0
    result = {
        "paths": paths,
        "seed": seed,
        "pv": base["valuation"]["pv"],
        "put_option_price": base["put_option_price"],
        "spot": [
            {
                "risk_factor": f"EQ:{name}:SPOT",
                "aad_fixed_branch": float(aad_deltas[i]),
                "pathwise": float(pathwise[i]),
                "crn_fd": float(fd[i]),
                "aad_minus_crn": float(aad_deltas[i] - fd[i]),
                "aad_vs_crn_pct": float((aad_deltas[i] - fd[i]) / max(abs(fd[i]), 1e-12) * 100.0),
            }
            for i, name in enumerate(market.names)
        ],
        "market_inputs": {
            "aad_available": aad_market.get("available", False),
            "aad_vega": aad_market.get("vega"),
            "crn_vega": crn_vega,
            "vega_difference": float(aad_market.get("vega", 0.0) - crn_vega) if aad_market.get("available") else None,
            "aad_irpv01": (
                -rate_h * float(aad_market.get("discount_sensitivity", 0.0)) if aad_market.get("available") else None
            ),
            "crn_irpv01": crn_irpv01,
            "aad_fx_delta": aad_market.get("fx_delta"),
            "fixture_fx_status": "not applicable: legacy fixture is USD/USD with no FX factor",
            "aad_skew_delta": aad_market.get("skew_delta"),
            "crn_skew_delta": crn_skew,
            "fixture_skew_status": "zero-skew control: no independent skew market input in legacy fixture",
        },
        "methods": base["explainability"]["risk_methods"],
    }
    Path("/tmp/fina-risk-fixture-aad-30k.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
