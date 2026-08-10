"""Application settings.

Every tunable value lives here so there is exactly one place to change limits,
paths and defaults. Each one can be overridden with an environment variable.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- Storage -----------------------------------------------------------------
DATA_DIR = Path(os.environ.get("COMPRESSOR_DATA_DIR", str(BASE_DIR / "data"))).resolve()
JOBS_DIR = DATA_DIR / "jobs"

# --- Limits ------------------------------------------------------------------
MAX_FILE_SIZE = _env_int("COMPRESSOR_MAX_FILE_MB", 250) * 1024 * 1024
MAX_FILES_PER_JOB = _env_int("COMPRESSOR_MAX_FILES", 50)
# Guards against decompression bombs while still allowing large real photos.
MAX_IMAGE_PIXELS = _env_int("COMPRESSOR_MAX_IMAGE_PIXELS", 150_000_000)

# --- Runtime -----------------------------------------------------------------
MAX_WORKERS = _env_int("COMPRESSOR_WORKERS", 4)
JOB_TTL_SECONDS = _env_int("COMPRESSOR_JOB_TTL_MIN", 60) * 60
CLEANUP_INTERVAL_SECONDS = 300
GHOSTSCRIPT_TIMEOUT = _env_int("COMPRESSOR_GS_TIMEOUT", 600)

# --- External tools ----------------------------------------------------------
# Ghostscript is optional. When missing, PDF compression falls back to a
# pure-Python path (pikepdf + Pillow) which is weaker but always available.
GHOSTSCRIPT_BIN = (
    os.environ.get("COMPRESSOR_GS_BIN")
    or shutil.which("gs")
    or shutil.which("gswin64c")
    or shutil.which("gswin32c")
)


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
