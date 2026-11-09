"""Every engine route is documented in API.md.

Collects ``METHOD /path`` from ``cc_engine.api``'s built-in route tables and from the ``ROUTES``
dict of every extension module in ``api.EXTENSIONS`` that imports (the engine skips the others
too). Each one must appear in the API reference: ``<repo>/core/docs/API.md`` in the source repo,
else ``<repo>/docs/API.md`` in the exported repo.
"""
import importlib
import re
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from cc_engine import api  # noqa: E402


def api_doc_path() -> Path:
    for candidate in (REPO / "core" / "docs" / "API.md", REPO / "docs" / "API.md"):
        if candidate.is_file():
            return candidate
    raise AssertionError("API.md not found under core/docs/ or docs/")


def all_routes() -> set:
    routes = {f"GET {p}" for p in api.GET_ROUTES} | {f"POST {p}" for p in api.POST_ROUTES}
    for name in api.EXTENSIONS:
        try:
            mod = importlib.import_module(f"cc_engine.{name}")
        except Exception:  # the engine skips a module that does not import; so does this test
            continue
        table = getattr(mod, "ROUTES", None)
        if not isinstance(table, dict):
            continue
        for key in table:
            if not isinstance(key, str):
                continue
            method, _, path = key.strip().partition(" ")
            method, path = method.upper(), path.strip()
            if method in ("GET", "POST") and path.startswith("/"):
                routes.add(f"{method} {path}")
    return routes


def documented(text: str) -> set:
    """``METHOD /path`` pairs written anywhere in the document (backticks optional)."""
    return {f"{m} {p}" for m, p in re.findall(r"\b(GET|POST) (/[A-Za-z0-9_./-]*)", text)}


def test_core_routes_found():
    routes = all_routes()
    assert "GET /health" in routes and "POST /api/node/status" in routes


def test_every_route_is_in_api_md():
    text = api_doc_path().read_text(encoding="utf-8")
    missing = sorted(all_routes() - documented(text))
    assert not missing, "routes missing from API.md: " + ", ".join(missing)


def test_api_md_names_the_api_version():
    text = api_doc_path().read_text(encoding="utf-8")
    assert "Waypoint API 1.1" in text
