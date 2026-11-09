"""Node ``files`` field: optional glob list per node, carried by import/export, the node view and versions."""
import copy
import sqlite3

import pytest

from cc_engine import plan, versions
from cc_engine.store import Store

from e2e_helpers import node, plan_dict


def _plan(files):
    return plan_dict("fp", "Files plan", [node("A", files=files), node("B", deps=["A"])])


def test_import_export_roundtrip_and_node_view(store):
    plan.import_plan(store, _plan(["engine/cc_engine/receipts.py", "docs/**"]))
    n = store.get_node("fp", "A")
    assert n["files"] == ["engine/cc_engine/receipts.py", "docs/**"] and "files_json" not in n
    assert store.get_node("fp", "B")["files"] == []
    out = plan.export_plan(store, "fp")
    by = {x["id"]: x for x in out["nodes"]}
    assert by["A"]["files"] == ["engine/cc_engine/receipts.py", "docs/**"] and "files" not in by["B"]
    plan.import_plan(store, copy.deepcopy(out))  # a round trip changes nothing
    assert plan.export_plan(store, "fp") == out


def test_reimport_without_files_keeps_them(store):
    plan.import_plan(store, _plan(["a/*.py"]))
    again = _plan(None)
    again["nodes"][0].pop("files")
    plan.import_plan(store, again)
    assert store.get_node("fp", "A")["files"] == ["a/*.py"]
    again["nodes"][0]["files"] = ["b/*.py"]
    plan.import_plan(store, again)
    assert store.get_node("fp", "A")["files"] == ["b/*.py"]


@pytest.mark.parametrize("bad", ["docs/**", [1], [""], ["x" * 201], ["../secret"], ["a/../b"], ["a" + chr(92) + ".." + chr(92) + "b"],
                                 ["f%d" % i for i in range(51)]])
def test_validation_refuses(store, bad):
    with pytest.raises(plan.PlanError):
        plan.import_plan(store, _plan(bad))


def test_validation_accepts_limits(store):
    plan.import_plan(store, _plan(["f%d" % i for i in range(50)] + []))
    plan.import_plan(store, _plan(["x" * 200]))
    plan.import_plan(store, _plan(["..hidden/ok*", "a..b"]))  # '..' only counts as a whole segment


def test_old_database_gets_the_column(tmp_path):
    path = tmp_path / "old.sqlite"
    s = Store(path)
    plan.import_plan(s, _plan(["a/*"]))
    s.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE nodes DROP COLUMN files_json")
    con.commit()
    con.close()
    s2 = Store(path)
    try:
        assert s2.get_node("fp", "A")["files"] == []
        s2.update_node("fp", "A", {"files": ["z/*"]}, "owner", "set files")
        assert s2.get_node("fp", "A")["files"] == ["z/*"]
    finally:
        s2.close()


def test_update_node_validates(store):
    plan.import_plan(store, _plan(None))
    with pytest.raises(ValueError):
        store.update_node("fp", "A", {"files": ["../x"]}, "owner", "bad")


def test_versions_carry_files(store):
    plan.import_plan(store, _plan(["core/**"]))
    out = versions.new_version(store, project="fp", actor="owner", expected_revision=store.revision())
    new = out["project"]
    assert store.get_node(new, "A")["files"] == ["core/**"]
