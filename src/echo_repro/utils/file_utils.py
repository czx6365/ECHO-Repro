from __future__ import annotations

from pathlib import Path


def read_text_safe(path: Path, max_chars: int = 2_000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except OSError:
        return ""

