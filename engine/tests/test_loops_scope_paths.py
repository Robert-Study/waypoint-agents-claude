"""Scope check edge cases found in review: string scopes and Windows path case."""
import os

import pytest

from cc_engine import loops


def test_scope_string_is_one_path_not_characters():
    assert loops.scope_of({"id": "a", "scope": "src"}) == ["src"]
    assert loops.scope_of({"id": "a", "scope": "src, docs/x.md"}) == ["src", "docs/x.md"]
    assert loops.scope_of({"id": "a", "scope": '["a.py", "b/"]'}) == ["a.py", "b/"]


def test_scope_string_passes_diff_check():
    ok, outside = loops.diff_check({"id": "a", "scope": "src"}, ["src/main.py"])
    assert ok and outside == []


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_diff_check_ignores_case_on_windows():
    ok, outside = loops.diff_check({"id": "a", "scope": ["Engine\CC_Engine"]}, ["engine/cc_engine/loops.py"])
    assert ok and outside == []


def test_diff_check_still_rejects_outside_files():
    ok, outside = loops.diff_check({"id": "a", "scope": ["src"]}, ["src/a.py", "srcx/b.py", "docs/c.md"])
    assert not ok and outside == ["srcx/b.py", "docs/c.md"]
