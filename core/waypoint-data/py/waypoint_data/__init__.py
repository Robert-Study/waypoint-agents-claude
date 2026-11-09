"""Python twin of the Waypoint data folder library (store, claims, paths, float). Stdlib only.

Rules live in core/docs/data/CONCURRENCY.md; the TypeScript fixtures keep this client identical to the TS one.
"""
from . import claims, float_, io, nodes, paths, store
from .claims import Claims, ClaimResult
from .float_ import compute_float
from .paths import PathError, resolve_data_root, to_cwd_ref, to_local_path
from .store import RevConflict, Store, serialize

__all__ = ["claims", "float_", "io", "nodes", "paths", "store", "Claims", "ClaimResult", "Store", "RevConflict", "serialize",
           "compute_float", "PathError", "resolve_data_root", "to_local_path", "to_cwd_ref"]
