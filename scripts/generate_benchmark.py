from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmark"
N_UNDERLYINGS = 1200
N_INSTRUMENTS = 2000
SEED = 20260909


def excel(d: date) -> int:
    return (d - date(1899, 12, 30)).days


def make_market() -> dict:
    rng = np.random.default_rng(SEED)
    names = [f"EQ{i:04d} US" for i in range(N_UNDERLYINGS)]
    currencies = ["USD", "EUR", "GBP", "JPY", "HKD", "SGD", "AUD", "CAD"]
    sectors = [f"SECTOR_{i:02d}" for i in range(24)]
    spots = rng.uniform(25.0, 850.0, N_UNDERLYINGS).round(6)
    ref = spots * rng.uniform(0.96, 1.04, N_UNDERLYINGS)
    sector = rng.integers(0, len(sectors), N_UNDERLYINGS)
    currency = rng.choice(currencies, N_UNDERLYINGS)
    underlyings = []
    for i, name in enumerate(names):
        divs = [
            {
                "exDate": excel(date(2026, 10, 15) + timedelta(days=91 * j)),
                "div": round(float(spots[i] * rng.uniform(0.0005, 0.003)), 6),
            }
            for j in range(5)
        ]
        underlyings.append(
            {
                "id": name,
                "currency": str(currency[i]),
                "sector": sectors[int(sector[i])],
                "spot": float(spots[i]),
                "referenceSpot": float(ref[i]),
                "bid": float(spots[i] - 0.01),
                "ask": float(spots[i] + 0.01),
                "dividend": divs,
                "calendar": "NYSE",
                "quoteConvention": "mid",
            }
        )
    maturities = [excel(date(2026, 10, 1) + timedelta(days=91 * i)) for i in range(1, 5)]
    vol_surfaces = []
    for i, name in enumerate(names):
        strikes = (spots[i] * np.array([0.70, 0.80, 0.90, 0.975, 1.0, 1.025, 1.10, 1.20, 1.30])).round(6).tolist()
        base = float(rng.uniform(0.18, 0.70))
        vols = [
            [round(max(0.08, base + 0.03 * (k - 4) ** 2 / 16 + 0.015 * m + rng.normal(0, 0.004)), 6) for k in range(9)]
            for m in range(4)
        ]
        vol_surfaces.append(
            {
                "underlying": name,
                "strikeType": "relative",
                "reference": float(spots[i]),
                "maturities": maturities,
                "strikes": strikes,
                "volatility": vols,
                "interpolation": "bilinear",
                "quoteUnit": "decimal",
            }
        )
    currencies_unique = sorted(set(str(x) for x in currency))
    fx = []
    fx_vol = []
    for _i, ccy in enumerate(currencies_unique):
        if ccy == "USD":
            continue
        fx.append(
            {
                "pair": f"{ccy}USD",
                "spot": round(float(rng.uniform(0.01, 1.5)), 8),
                "bidAsk": 0.0001,
                "quote": "domestic per USD",
            }
        )
        fx_vol.append(
            {
                "pair": f"{ccy}USD",
                "maturities": maturities,
                "volatility": [round(float(rng.uniform(0.05, 0.22)), 6) for _ in maturities],
            }
        )
    curve_dates = [excel(date(2026, 10, 1) + timedelta(days=91 * i)) for i in range(1, 13)]
    curves = [
        {
            "id": f"{ccy} OIS",
            "currency": ccy,
            "dayCount": "Actual/365",
            "compounding": "continuous",
            "pillars": [{"date": d, "rate": round(float(0.015 + 0.025 * rng.random()), 8)} for d in curve_dates],
        }
        for ccy in currencies_unique
    ]
    factors = rng.normal(size=(N_UNDERLYINGS, 12))
    factors /= np.linalg.norm(factors, axis=1, keepdims=True)
    corr = factors @ factors.T
    corr = 0.25 * corr + 0.75 * np.eye(N_UNDERLYINGS)
    for i in range(N_UNDERLYINGS):
        for j in range(i):
            if sector[i] == sector[j]:
                corr[i, j] += 0.18
                corr[j, i] = corr[i, j]
    corr = np.clip(corr, -0.95, 0.95)
    np.fill_diagonal(corr, 1.0)
    return {
        "schemaVersion": "benchmark.market.v1",
        "evaluationDate": excel(date(2026, 9, 9)),
        "seed": SEED,
        "underlyings": underlyings,
        "fx": {"spots": fx, "volSurfaces": fx_vol},
        "interestCurves": curves,
        "volSurfaces": vol_surfaces,
        "correlationMatrix": {
            "factorIds": names,
            "dimension": N_UNDERLYINGS,
            "values": np.round(corr, 6).tolist(),
            "construction": "12-factor sector-enhanced PSD reference matrix",
        },
        "simulation": {
            "paths": 30000,
            "steps": 252,
            "factorCount": 12,
            "commonRandomNumbers": True,
            "backendPreference": ["gpu", "simd_cpu", "scalar_cpu"],
        },
    }


def make_instruments(market: dict) -> dict:
    rng = np.random.default_rng(SEED + 1)
    names = [x["id"] for x in market["underlyings"]]
    instruments = []
    for i in range(N_INSTRUMENTS):
        under = [names[(3 * i + j * 137 + (i % 11)) % N_UNDERLYINGS] for j in range(3)]
        notional = float(rng.choice([100000, 250000, 500000, 1000000]))
        strike = round(float(rng.uniform(0.70, 0.90)), 6)
        barrier = round(float(rng.uniform(0.60, 0.80)), 6)
        call = round(float(rng.uniform(1.05, 1.20)), 6)
        dates = [excel(date(2026, 10, 1) + timedelta(days=30 * j)) for j in range(1, 10)]
        legs = [
            {
                "legId": 1,
                "legType": "intrinsic_option",
                "legName": "PUT",
                "multiplier": -1,
                "payoff": {
                    "basket": "worst_of",
                    "strike": strike,
                    "knockIn": {"monitoring": "EKI", "barrier": barrier},
                    "settlement": "physical_delivery",
                },
            },
            {
                "legId": 3,
                "legType": "funding",
                "legName": "FUNDING",
                "multiplier": 1,
                "payoff": {"notionalReturn": True, "returnRatio": 1.0},
            },
            {
                "legId": 2,
                "legType": "coupon",
                "legName": "COUPON",
                "multiplier": 1,
                "payoff": {
                    "type": "range_accrual_strip",
                    "indicator": "WPS",
                    "rate": round(float(rng.uniform(0.006, 0.018)), 8),
                    "floor": round(float(rng.uniform(0.05, 0.20)), 6),
                    "unpaidPeriodRule": "N2-N1",
                    "paymentLagDays": [2, 2, 2, 2, 2, 2, 2, 2, 2],
                },
            },
        ]
        instruments.append(
            {
                "instrumentId": f"ELI-BENCH-{i:05d}",
                "productType": "ELIFCN_KI",
                "currency": str(market["underlyings"][i % N_UNDERLYINGS]["currency"]),
                "notional": notional,
                "underlyings": under,
                "initialFixingDate": market["evaluationDate"],
                "finalFixingDate": dates[-1],
                "maturityDate": dates[-1] + 2,
                "features": {
                    "worstOf": True,
                    "globalMemoryCall": bool(i % 3),
                    "callBarrier": call,
                    "kiMonitoring": "EKI",
                    "physicalDelivery": True,
                    "memoryLocked": [bool(i % 4 == 0), False, bool(i % 7 == 0)],
                },
                "legs": legs,
                "legacySemantics": {
                    "legOrder": ["PUT", "FUNDING", "COUPON"],
                    "quotedSpotRelativeBump": 0.01,
                    "commonRandomNumbers": True,
                    "sourceFamily": "augmented_fixture",
                },
            }
        )
    return {
        "schemaVersion": "benchmark.instruments.v1",
        "count": len(instruments),
        "averageLegs": round(sum(len(x["legs"]) for x in instruments) / len(instruments), 2),
        "underlyingCount": N_UNDERLYINGS,
        "instruments": instruments,
        "reusePlan": {
            "marketSnapshot": "shared",
            "correlationMatrix": "shared",
            "volSurfaces": "reverse-indexed by underlying",
            "pathCube": "shared by batch and seed",
            "structureCache": "group by leg/payoff signature",
        },
    }


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    market = make_market()
    instruments = make_instruments(market)
    (OUT / "market.json").write_text(json.dumps(market, separators=(",", ":")))
    (OUT / "instruments.json").write_text(json.dumps(instruments, separators=(",", ":")))
    print(
        json.dumps(
            {
                "market": str(OUT / "market.json"),
                "instruments": str(OUT / "instruments.json"),
                "underlyings": N_UNDERLYINGS,
                "instruments_count": N_INSTRUMENTS,
            },
            indent=2,
        )
    )
