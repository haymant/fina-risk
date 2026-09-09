from __future__ import annotations

import json
from pathlib import Path

from fina_risk.pricing import common_from_job, load_legacy_request, price_fixture
from fina_risk.server import _execute


def main() -> None:
    request = json.loads(Path("skills/fina-risk/refs/termsheet1.md.json").read_text())
    jobs = load_legacy_request(request)
    paths = 30_000
    seed = 1729
    job_results = [price_fixture(common_from_job(job), paths=paths, seed=seed) for job in jobs]
    canonical = _execute("pricing_and_sensitivity", {"paths": paths, "seed": seed, "request": request})
    print(
        json.dumps(
            {
                "job_count": len(jobs),
                "job_pvs": [result["valuation"]["pv"] for result in job_results],
                "job_put_prices": [result["put_option_price"] for result in job_results],
                "job_leg_pvs": [[leg["pv"] for leg in result["legs"]] for result in job_results],
                "single_first_job_pv": job_results[0]["valuation"]["pv"],
                "canonical_server_pv": canonical["base"]["valuation"]["pv"],
                "canonical_server_legs": canonical["base"]["legs"],
                "canonical_coupon_explain": canonical["base"]["explainability"].get("coupon"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
