from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    import _cffi_backend  # noqa: F401
except ModuleNotFoundError:
    uv_cache = Path.home() / ".cache" / "uv" / "archive-v0"
    if uv_cache.is_dir():
        for so_file in uv_cache.glob("*/_cffi_backend*.so"):
            sys.path.append(str(so_file.parent))
            break


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_tex: marks integration/E2E tests that require a TeX Live installation",
    )
