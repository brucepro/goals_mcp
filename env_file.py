from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> int:
    p = Path(path or Path(__file__).parent / ".env")
    if not p.exists():
        return 0

    applied = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            applied += 1
    return applied
