from __future__ import annotations

import json
from pathlib import Path

from fina_risk.pipeline import ingest_instruments, ingest_market_data


def main() -> None:
    source = Path("/tmp/fina-risk-100k-input")
    target = Path("/tmp/fina-risk-100k-input-olap")
    target.mkdir(parents=True, exist_ok=True)
    instruments = json.loads((source / "instruments.json").read_text())
    market = json.loads((source / "market.json").read_text())
    print(
        json.dumps(
            {
                "instruments": ingest_instruments(instruments, root=target),
                "market": ingest_market_data(market, root=target),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
