import json
import threading
import urllib.request

import pytest

from cc_engine import app_shell
from cc_engine.api import make_server


@pytest.fixture
def base(store):
    srv = make_server(store, port=0, poll_interval=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers, r.read()


def test_app_shell_routes(base):
    s, h, b = get(base + "/app/")
    assert s == 200 and b"/api/stream" in b and b"manifest" in b
    s, h, b = get(base + "/app/manifest.webmanifest")
    m = json.loads(b)
    assert m["display"] == "standalone" and m["start_url"] == "/app/"
    s, h, b = get(base + "/app/icon-256.png")
    assert b[:4] == b"\x89PNG"
    s, h, b = get(base + "/app/sw.js")
    assert b"fetch(e.request)" in b


def test_service_worker_never_caches():
    assert "caches." not in app_shell.SW and "cache.put" not in app_shell.SW.lower()


def test_history_section_and_code(base):
    s, h, b = get(base + "/app/")
    t = b.decode("utf-8")
    assert 'id="pj"' in t and ">Projects<" in t
    assert "/api/history?family=" in t and "/api/projects" in t and "History" in t
    assert "Skipped ideas" in t and "createElement" in t


def test_history_escapes_api_text():
    page = app_shell.PAGE
    js = page.split("<script>")[1]
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert "textContent" in js and "encodeURIComponent" in js
    assert "method" not in js.lower().split("loadprojects")[1]  # read-only: no writes


def test_state_summary_names_object_projects():
    # The SSE state payload's project is an object ({id, title, ...}); the summary shows its title.
    from cc_engine import app_shell
    assert "typeof pj==='object'" in app_shell.PAGE


def test_app_works_without_plugin_assets(base, monkeypatch, tmp_path):
    """The Waypoint Core export has no plugin/assets: /app/, the manifest and both icons still answer."""
    monkeypatch.setattr(app_shell, "ASSETS", tmp_path / "no-such-assets")
    s, h, b = get(base + "/app/")
    assert s == 200 and b"/api/stream" in b and b"manifest" in b
    s, h, b = get(base + "/app/manifest.webmanifest")
    m = json.loads(b)
    assert [i["src"] for i in m["icons"]] == ["/app/icon-256.png", "/app/icon-128.png"]
    import struct
    import zlib
    for size in (256, 128):
        s, h, b = get(base + f"/app/icon-{size}.png")
        assert s == 200 and h["Content-Type"] == "image/png" and b[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", b[16:24]) == (size, size)
        idat = b[b.index(b"IDAT") + 4:]
        assert len(zlib.decompress(idat[:struct.unpack(">I", b[b.index(b"IDAT") - 4:b.index(b"IDAT")])[0]])) == size * (size * 4 + 1)
