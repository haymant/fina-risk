"""ETL: augment a legacy term sheet into a permuted batch and compile it to the
fina-risk pricing-request schema.

The bundled reference input is ``skills/fina-risk/refs/termsheet1.md.json``
(``Chunk.Jobs[]`` → ``commonData.dealData`` + ``commonData.marketData``). The
permutation model mirrors ``scripts/generate_benchmark.py``: each variant keeps
the ELI structure (PUT / FUNDING / COUPON legs) but randomises economics —
relative strike, knock-in barrier, call barrier, notional, underlyings/spot,
coupon rate and the expiry/maturity calendar — deterministically from a seed.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TERMSHEET = _REPO_ROOT / "skills/fina-risk/refs/termsheet1.md.json"
DEFAULT_SCHEMA = _REPO_ROOT / "skills/fina-risk/schema/pricing-request.schema.json"

NOTIONAL_CHOICES = (100_000.0, 250_000.0, 500_000.0, 1_000_000.0)
_DATE_LIST_FIELDS = (
    ("globalKOStar", ("obvDate", "effectiveDate")),
    ("RGACCLKO", ("endDate", "paymentDate", "startDate")),
    ("knockInStar", ("obvDate", "paymentDate")),
)


def load_termsheet(source: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a legacy term-sheet request from a path, or pass a dict through."""
    if isinstance(source, dict):
        return copy.deepcopy(source)
    path = Path(source) if source else DEFAULT_TERMSHEET
    return json.loads(path.read_text())


def _jobs(termsheet: dict[str, Any]) -> list[dict[str, Any]]:
    return termsheet["Chunk"]["Jobs"]


def _leg_job(termsheet: dict[str, Any], leg_name: str) -> dict[str, Any]:
    for job in _jobs(termsheet):
        if job["commonData"]["dealData"].get("legName") == leg_name:
            return job
    raise ValueError(f"leg {leg_name!r} not present in term sheet")


def _shift(values: Any, days: int) -> Any:
    if isinstance(values, list):
        return [int(v) + days if isinstance(v, (int, float)) else v for v in values]
    if isinstance(values, (int, float)):
        return int(values) + days
    return values


def _apply_underlyings(kiko: dict[str, Any], chosen: list[dict[str, Any]]) -> None:
    kiko["underlying"] = [e["_id"] for e in chosen]
    kiko["underlyingName"] = [e["_id"] for e in chosen]
    kiko["referencePrice"] = [round(float(e.get("spot", 0.0)), 6) for e in chosen]
    kiko["underlyingMarket"] = [str(e.get("market", "US")) for e in chosen]
    kiko["underlyingCcy"] = [str(e.get("currency", "USD")) for e in chosen]
    kiko["FXPair"] = [f"{e.get('currency', 'USD')}USD" for e in chosen]


def _permute_variant(
    base: dict[str, Any],
    *,
    index: int,
    strike: float,
    barrier: float,
    call: float,
    notional: float,
    coupon: float,
    chosen: list[dict[str, Any]],
    shift_days: int,
) -> dict[str, Any]:
    variant = copy.deepcopy(base)
    for job in _jobs(variant):
        deal = job["commonData"]["dealData"]
        leg = deal.get("legName")
        deal["notional"] = notional
        if isinstance(deal.get("instrument"), dict):
            deal["instrument"]["notional"] = notional
        deal["instrumentName"] = f"{deal.get('instrumentName', 'ELIFCN')}-{index:05d}"
        deal["_id"] = f"{deal.get('_id', 'ELIFCN')}-{index:05d}"
        deal["expiryDate"] = _shift(deal.get("expiryDate"), shift_days)
        deal["maturityDate"] = _shift(deal.get("maturityDate"), shift_days)
        for section, fields in _DATE_LIST_FIELDS:
            block = deal.get(section)
            if isinstance(block, dict):
                for field in fields:
                    if field in block:
                        block[field] = _shift(block[field], shift_days)
        kiko = deal.get("KIKOSelect")
        if isinstance(kiko, dict):
            _apply_underlyings(kiko, chosen)
        if leg == "PUT":
            deal["strike"] = strike
            kis = deal.get("knockInStar")
            if isinstance(kis, dict):
                kis["KIBarrier"] = barrier
                kis["strikeKI2"] = strike
                kis["maturBarrier"] = strike
            rgacc = deal.get("RGACCLKO")
            if isinstance(rgacc, dict):
                rgacc["gblBarPrice"] = call
        elif leg == "FUNDING":
            if isinstance(kiko, dict):
                kiko["notionalReturn"] = True
                kiko["returnRatio"] = 1.0
        elif leg == "COUPON":
            rgacc = deal.get("RGACCLKO")
            if isinstance(rgacc, dict):
                rates = list(rgacc.get("accruRate", [0.0]))
                rgacc["accruRate"] = [0.0, *[coupon] * max(len(rates) - 1, 0)]
    return variant


def augment_termsheet(
    source: str | Path | dict[str, Any] | None = None,
    *,
    count: int = 10,
    seed: int = 20260909,
) -> list[dict[str, Any]]:
    """Return ``count`` term-sheet variants with permuted economics (seeded)."""
    if count < 1:
        raise ValueError("count must be >= 1")
    base = load_termsheet(source)
    rng = np.random.default_rng(seed)
    market = _jobs(base)[0]["commonData"]["marketData"]
    equities = [
        {
            "_id": e["_id"],
            "spot": e.get("spot", 0.0),
            "currency": e.get("currency", "USD"),
            "market": e.get("market", "US"),
        }
        for e in market.get("equity", [])
    ] or [{"_id": "ADBE UW", "spot": 100.0, "currency": "USD", "market": "US"}]
    base_strike = float(_leg_job(base, "PUT")["commonData"]["dealData"].get("strike", 0.9))

    variants: list[dict[str, Any]] = []
    for i in range(count):
        strike = round(base_strike * float(rng.uniform(0.85, 1.05)), 6)
        barrier = round(strike * float(rng.uniform(0.80, 0.95)), 6)
        call = round(float(rng.uniform(1.02, 1.20)), 6)
        notional = float(rng.choice(NOTIONAL_CHOICES))
        coupon = round(float(rng.uniform(0.006, 0.018)), 6)
        k = int(min(max(int(rng.integers(2, 4)), 2), len(equities))) if len(equities) >= 2 else 1
        picked = rng.choice(len(equities), size=k, replace=False)
        chosen = [equities[int(j)] for j in np.atleast_1d(picked)]
        shift_days = int(rng.integers(-10, 11))
        variants.append(
            _permute_variant(
                base,
                index=i,
                strike=strike,
                barrier=barrier,
                call=call,
                notional=notional,
                coupon=coupon,
                chosen=chosen,
                shift_days=shift_days,
            )
        )
    return variants


def _first_nonzero(values: Any, default: float = 0.0) -> float:
    if isinstance(values, list):
        for v in values:
            if isinstance(v, (int, float)) and v:
                return float(v)
    return default


def _market_snapshot(market: dict[str, Any], reference: list[float]) -> dict[str, Any]:
    underlyings = [
        {
            "id": e["_id"],
            "quoted_spot": round(float(e.get("spot", 0.0)), 6),
            "reference_spot": round(float(e.get("spot", 0.0)), 6),
            "bid": e.get("bid"),
            "ask": e.get("ask"),
            "currency": e.get("currency", "USD"),
            "dividends": [
                {"ex_date": d.get("exDate", d.get("ex_date")), "amount": d.get("div", d.get("amount"))}
                for d in e.get("dividend", [])
                if isinstance(d, dict)
            ],
        }
        for e in market.get("equity", [])
    ]
    curves = [
        {"id": c.get("_id", "curve"), "day_count": c.get("convention", "Actual/365"), "pillars": c.get("curve", [])}
        for c in market.get("discCurves", [])
    ]
    maturity_dates = [p["date"] for p in (market.get("discCurves") or [{}])[0].get("curve", [])]
    vol_surfaces = []
    for idx, v in enumerate(market.get("eqVol", [])):
        vols = v.get("vol", [])
        mats = maturity_dates[: len(vols)] or list(range(len(vols)))
        ref = reference[idx] if idx < len(reference) else (reference[0] if reference else 0.0)
        vol_surfaces.append(
            {
                "underlying": v.get("underlying") or v.get("_id") or f"U{idx}",
                "strike_type": "absolute",
                "reference": float(ref),
                "maturities": mats,
                "strikes": v.get("strike", []),
                "volatility": vols,
            }
        )
    correlations = [
        {"factors": c.get("_id", []), "value": [x.get("correlation", 0.0) for x in c.get("correlation", [])]}
        for c in market.get("corr", [])
    ]
    return {
        "evaluation_date": market.get("evaluationDate"),
        "underlyings": underlyings,
        "curves": curves,
        "vol_surfaces": vol_surfaces,
        "correlations": correlations,
    }


def compile_pricing_request(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Compile a legacy term sheet into the fina-risk ``pricing-request`` schema."""
    termsheet = load_termsheet(source)
    market = _jobs(termsheet)[0]["commonData"]["marketData"]
    put = _leg_job(termsheet, "PUT")["commonData"]["dealData"]
    funding = _leg_job(termsheet, "FUNDING")["commonData"]["dealData"]
    coupon = _leg_job(termsheet, "COUPON")["commonData"]["dealData"]
    kiko = put.get("KIKOSelect", {})
    reference = [float(x) for x in kiko.get("referencePrice", [])]
    coupon_rgacc = coupon.get("RGACCLKO", {})
    mcpara = market.get("MCPara", {})

    legs = [
        {
            "leg_id": 1,
            "leg_type": "intrinsic_option",
            "leg_name": "PUT",
            "multiplier": -1,
            "notional": float(put.get("notional", 0)),
            "payoff": {
                "basket": "worst_of",
                "strike": float(put.get("strike", 0)),
                "knock_in": {"monitoring": "EKI", "barrier": float(put.get("knockInStar", {}).get("KIBarrier", 0))},
                "settlement": "physical_delivery",
            },
        },
        {
            "leg_id": 3,
            "leg_type": "funding",
            "leg_name": "FUNDING",
            "multiplier": 1,
            "notional": float(funding.get("notional", 0)),
            "payoff": {
                "notional_return": True,
                "return_ratio": float(funding.get("KIKOSelect", {}).get("returnRatio", 1.0)),
            },
        },
        {
            "leg_id": 2,
            "leg_type": "coupon",
            "leg_name": "COUPON",
            "multiplier": 1,
            "notional": float(coupon.get("notional", 0)),
            "payoff": {
                "type": "range_accrual_strip",
                "indicator": coupon_rgacc.get("accIndicator", "WPS"),
                "rate": _first_nonzero(coupon_rgacc.get("accruRate", [])),
                "unpaid_period_rule": "N2-N1",
                "payment_lag_days": [2] * max(len(coupon_rgacc.get("endDate", [])), 1),
            },
        },
    ]

    return {
        "instrument_key": str(put.get("_id") or put.get("instrumentName") or "instrument"),
        "market_data": _market_snapshot(market, reference),
        "parameters": {
            "bump_size": 0.01,
            "bump_mode": "relative",
            "method_priority": ["AAD", "PATHWISE", "LRM", "FD"],
            "seed": int(market.get("seed", 1729)),
            "common_random_numbers": bool(mcpara.get("commonRandomNumbers", True)),
            "paths": int(mcpara.get("numPaths", 30000)),
            "steps": 252,
        },
        "common_economics": {
            "notional": float(put.get("notional", 0)),
            "payment_currency": put.get("paymentCurrency", "USD"),
            "underlyings": kiko.get("underlying", []),
            "product_type": put.get("instrument", {}).get("type", "ELIFCN"),
        },
        "legs": legs,
    }


def validate_pricing_request(request: dict[str, Any], schema_path: str | Path | None = None) -> None:
    """Validate against the pricing-request schema when jsonschema + schema exist."""
    path = Path(schema_path) if schema_path else DEFAULT_SCHEMA
    if not path.exists() or importlib.util.find_spec("jsonschema") is None:
        return
    import jsonschema

    jsonschema.validate(request, json.loads(path.read_text()))


__all__ = [
    "DEFAULT_SCHEMA",
    "DEFAULT_TERMSHEET",
    "NOTIONAL_CHOICES",
    "augment_termsheet",
    "compile_pricing_request",
    "load_termsheet",
    "validate_pricing_request",
]
