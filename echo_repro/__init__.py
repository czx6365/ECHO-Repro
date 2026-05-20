from __future__ import annotations

from pathlib import Path

__all__ = ["__version__"]

__version__ = "0.1.0"

_src_package_dir = Path(__file__).resolve().parent.parent / "src" / "echo_repro"
if _src_package_dir.exists():
    __path__.append(str(_src_package_dir))
