"""Waypoint engine (stdlib only): the plan graph, its HTTP API and the scheduler behind Waypoint Core.

Run ``python -m cc_engine serve`` from ``engine/``. The HTTP API version is ``cc_engine.api.API_VERSION``."""
import os
from pathlib import Path

from . import config as _config

__version__ = "1.2.0"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(_config.DATA_DIR)


def default_db_path() -> Path:
    """Live SQLite path: ``$CC_DB`` or ``<repo>/data/cc.sqlite`` (git-ignored)."""
    return Path(os.environ.get("CC_DB") or DATA_DIR / "cc.sqlite")
