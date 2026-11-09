"""The /app/ page has a read-only Agents section: the live tree from /api/trace/tree. Dispatch lives in the mod."""
import threading
import urllib.request

import pytest

from cc_engine import app_shell
from cc_engine.api import make_server


@pytest.fixture
def page(store):
    srv = make_server(store, port=0, poll_interval=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/app/") as r:
        body = r.read().decode("utf-8")
    srv.shutdown()
    return body


def test_agents_section_and_endpoints(page):
    assert 'id="ag"' in page and ">Agents<" in page
    assert "/api/trace/tree" in page and "'trace'" in page and "15000" in page
    assert "Waypoint pane in Claude" in page
    assert "/api/dispatch/headless" not in page and "Owner token" not in page


def test_row_parts_present(page):
    js = page.split("<script>")[1]
    for part in ("tokens_in", "tokens_out", "context_window", "last_line", "transcript", "file:", "tool"):
        assert part in js


def test_tree_code_is_safe_and_plain():
    js = app_shell.PAGE.split("<script>")[1]
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert "!important" not in app_shell.PAGE
    # the page is read-only: it never writes and never asks for a token
    assert "method:'POST'" not in js and "X-CC-Owner-Token" not in js


def test_one_filled_button_per_view(page):
    head = page.split("</style>")[0]
    assert "button.sec" in head  # the dispatch buttons are outlined, the board link stays the one filled button
