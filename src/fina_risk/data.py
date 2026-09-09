from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any


def load_json_source(source: str | Path) -> dict[str, Any]:
    """Load JSON from a file or a virtual ``archive.zip/member.json`` path."""
    text = str(source)
    path = Path(text)
    if path.exists():
        return json.loads(path.read_text())
    marker = text.lower().find(".zip/")
    if marker >= 0:
        archive = Path(text[: marker + 4])
        member = text[marker + 5 :]
        with zipfile.ZipFile(archive) as zf:
            return json.loads(zf.read(member))
    raise FileNotFoundError(text)
