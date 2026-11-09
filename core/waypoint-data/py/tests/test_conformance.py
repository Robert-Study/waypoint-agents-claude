"""Conformance of the Python data client with the TypeScript fixtures and docs/data/CONCURRENCY.md.

Stdlib only. Run: <python> -m pytest core/waypoint-data/py/tests -q
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PY_ROOT = HERE.parent
sys.path.insert(0, str(PY_ROOT))
FIXTURES = PY_ROOT.parent / "fixtures"
SAMPLE = FIXTURES / "sample-folder"

from waypoint_data import claims as claims_mod  # noqa: E402
from waypoint_data import float_ as float_mod  # noqa: E402
from waypoint_data import io as wio  # noqa: E402
from waypoint_data import paths as wpaths  # noqa: E402
from waypoint_data import store as wstore  # noqa: E402


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- paths.json

PATHS = load("paths.json")


@pytest.mark.parametrize("c", PATHS["resolve_data_root"], ids=lambda c: c["name"])
def test_paths_resolve_data_root(c):
    got = wpaths.resolve_data_root(env=c["env"], home=c["home"], read_file=lambda p: c["fs"].get(p), platform="win32")
    assert got == c["expect"]


@pytest.mark.parametrize("c", PATHS["to_local_path"], ids=lambda c: c["name"])
def test_paths_to_local_path(c):
    if "expect_error" in c:
        with pytest.raises(wpaths.PathError) as e:
            wpaths.to_local_path(c["cwd"], c["machines"], "win32")
        assert e.value.code == c["expect_error"]
    else:
        assert wpaths.to_local_path(c["cwd"], c["machines"], "win32") == c["expect"]


@pytest.mark.parametrize("c", PATHS["to_cwd_ref"], ids=lambda c: c["name"])
def test_paths_to_cwd_ref(c):
    if "expect_error" in c:
        with pytest.raises(wpaths.PathError) as e:
            wpaths.to_cwd_ref(c["machine"], c["code_root"], c["abs"], "win32")
        assert e.value.code == c["expect_error"]
    else:
        assert wpaths.to_cwd_ref(c["machine"], c["code_root"], c["abs"], "win32") == c["expect"]


def test_paths_parse_machine_file():
    assert wpaths.parse_machine_file('{"code_root": "C:\\\\x"}') == "C:\\x"
    for bad in ("nope", "{}", '{"code_root": " "}'):
        with pytest.raises(wpaths.PathError) as e:
            wpaths.parse_machine_file(bad)
        assert e.value.code == "bad_machine_file"


# ---------------------------------------------------------------- float.json

FLOAT = load("float.json")


@pytest.mark.parametrize("c", FLOAT["cases"], ids=lambda c: c["name"])
def test_float_cases(c):
    if "expect_error" in c:
        with pytest.raises(ValueError) as e:
            float_mod.compute_float(c["nodes"])
        assert str(e.value).startswith(c["expect_error"] + ":")
        return
    got = {r["id"]: r for r in float_mod.compute_float(c["nodes"])}
    assert set(got) == {n["id"] for n in c["nodes"]}
    for nid, exp in c["expect"].items():
        g = got[nid]
        assert (g["early"], g["late"], g["float"], g["critical"]) == (
            exp["early"], exp["late"], exp["float"], exp["critical"]), nid


# ---------------------------------------------------------------- store

def node(**kw):
    d = {"schema": "waypoint.node.v1", "id": "n1", "title": "T", "rev": 0,
         "updated_at": "2026-11-07T12:00:00Z", "updated_by": "test"}
    d.update(kw)
    return d


def sample_files():
    return sorted(p for p in SAMPLE.rglob("*.json"))


def test_serialize_matches_js_json_stringify():
    s = wstore.serialize
    assert s({"a": 1.0, "b": [], "c": {}, "d": "\u00e9\u2028", "e": [1, 2.5]}) == (
        '{\n  "a": 1,\n  "b": [],\n  "c": {},\n  "d": "\u00e9\u2028",\n  "e": [\n    1,\n    2.5\n  ]\n}\n')


def test_sample_folder_round_trips_byte_stable(tmp_path):
    files = sample_files()
    assert len(files) > 20
    for f in files:
        rel = f.relative_to(SAMPLE).as_posix()
        entity = json.loads(f.read_text(encoding="utf-8"))
        root = tmp_path / rel.replace("/", "_")
        st = wstore.Store(str(root))
        written = st.write(rel, entity)
        back = st.read(rel)
        assert back == written, rel
        data = (root / rel).read_bytes()
        assert data.endswith(b"\n") and b"\r" not in data, rel
        assert st.serialize(back).encode("utf-8") == data, rel
        assert st.serialize(json.loads(st.serialize(back))).encode("utf-8") == data, rel


def test_create_bumps_rev_and_update_bumps(tmp_path):
    st = wstore.Store(str(tmp_path), writer="runner")
    w = st.write("projects/p/nodes/n1.json", node())
    assert w["rev"] == 1 and w["updated_by"] == "runner"
    assert st.read("projects/p/nodes/n1.json")["rev"] == 1
    w2 = st.write("projects/p/nodes/n1.json", node(title="T2"), expected_rev=1)
    assert w2["rev"] == 2


def test_stale_rev_rejected_and_file_unchanged(tmp_path):
    st = wstore.Store(str(tmp_path))
    st.write("n.json", node())
    st.write("n.json", node(title="T2"), expected_rev=1)
    before = (tmp_path / "n.json").read_bytes()
    with pytest.raises(wstore.RevConflict) as e:
        st.write("n.json", node(title="T3"), expected_rev=1)
    assert e.value.current["rev"] == 2
    assert (tmp_path / "n.json").read_bytes() == before


def test_create_with_expected_rev_zero_is_exclusive(tmp_path):
    st = wstore.Store(str(tmp_path))
    st.write("n.json", node(), expected_rev=0)
    with pytest.raises(wstore.RevConflict):
        st.write("n.json", node(), expected_rev=0)


def test_two_threads_one_rev_exactly_one_wins(tmp_path):
    st = wstore.Store(str(tmp_path))
    st.write("n.json", node())
    barrier = threading.Barrier(2)
    out = []

    def go(title):
        barrier.wait()
        try:
            out.append(("ok", st.write("n.json", node(title=title), expected_rev=1)))
        except wstore.RevConflict as e:
            out.append(("conflict", e))

    ts = [threading.Thread(target=go, args=(t,)) for t in "AB"]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(k for k, _ in out) == ["conflict", "ok"]
    assert st.read("n.json")["rev"] == 2
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith((".tmp", ".lock"))] == []


def test_noop_write_does_not_bump_rev(tmp_path):
    st = wstore.Store(str(tmp_path))
    st.write("n.json", node())
    again = st.write("n.json", node(), expected_rev=1)
    assert again["rev"] == 1


def test_unknown_fields_preserved(tmp_path):
    st = wstore.Store(str(tmp_path))
    st.write("n.json", node(future={"x": [1, 2]}))
    assert st.read("n.json")["future"] == {"x": [1, 2]}


def test_too_large_entity_rejected(tmp_path):
    st = wstore.Store(str(tmp_path))
    with pytest.raises(wio.FileTooLarge):
        st.write("n.json", node(blob="x" * (wio.MAX_BYTES + 1)))
    assert not (tmp_path / "n.json").exists()


def test_torn_read_retries_then_reports(tmp_path):
    (tmp_path / "n.json").write_text("{", encoding="utf-8")
    st = wstore.Store(str(tmp_path), torn_retry_s=0.001)
    with pytest.raises(wio.ReadTorn):
        st.read("n.json")


def test_torn_read_recovers_when_writer_finishes(tmp_path):
    p = tmp_path / "n.json"
    p.write_text("", encoding="utf-8")
    st = wstore.Store(str(tmp_path), torn_retry_s=0.05)
    threading.Timer(0.08, lambda: p.write_text(json.dumps(node()), encoding="utf-8")).start()
    assert st.read("n.json")["id"] == "n1"


def test_atomic_write_retries_locked_rename(tmp_path, monkeypatch):
    target = str(tmp_path / "a.json")
    calls = {"n": 0}
    real = os.replace

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 4:
            raise PermissionError(13, "held open")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(wio, "_sleep", lambda s: None)
    wio.atomic_write(target, "{}\n", writer="t")
    assert calls["n"] == 4 and Path(target).read_text() == "{}\n"


def test_atomic_write_write_locked_cleans_temp(tmp_path, monkeypatch):
    def always(a, b):
        raise PermissionError(13, "held")

    sleeps = []
    monkeypatch.setattr(os, "replace", always)
    monkeypatch.setattr(wio, "_sleep", lambda s: sleeps.append(s))
    with pytest.raises(wio.WriteLocked):
        wio.atomic_write(str(tmp_path / "a.json"), "{}\n", writer="t")
    assert list(tmp_path.iterdir()) == []
    assert len(sleeps) == 8


# ------------------------------------------------- ignore temp / conflicted

IGNORED = [".a.json.runner.1.abcd1234.tmp", "a.json.tmp", "a.json.lock", "a.json.partial", "a.json.stale.tok",
           "a (1).json", "a (12).json", "a - Copy.json", "a - Copy (2).json",
           "a (conflicted copy PC1 2026-11-07).json", "a (Sync conflict).json", ".hidden.json"]


@pytest.mark.parametrize("name", IGNORED)
def test_ignored_names(name):
    assert wio.is_ignored_name(name)


@pytest.mark.parametrize("name", ["a.json", "waypoint-v6.json", "data-paths.json", "x.ndjson"])
def test_entity_names_not_ignored(name):
    assert not wio.is_ignored_name(name)


def test_every_reader_ignores_junk_names(tmp_path):
    st = wstore.Store(str(tmp_path))
    st.write("claims/good.json", node(id="good"))
    for n in IGNORED:
        (tmp_path / "claims" / n).write_text(json.dumps(node(id="bad", rev=99)), encoding="utf-8")
    assert st.list_names("claims") == ["good.json"]
    assert [e["id"] for e in st.list_entities("claims")] == ["good"]
    cl = claims_mod.Claims(str(tmp_path))
    assert cl.read("p", "good (1)") is None
    assert [c["id"] for c in cl.list_claims()] == ["good"]


def test_quarantine_moves_conflicted_copies_never_deletes(tmp_path):
    st = wstore.Store(str(tmp_path), writer="runner")
    st.write("projects/p.json", node(id="live"))
    conflicted = tmp_path / "projects" / "p (1).json"
    conflicted.write_text(json.dumps(node(id="live", rev=5)), encoding="utf-8")
    moved = st.quarantine_conflicts("projects")
    assert len(moved) == 1 and not conflicted.exists()
    q = tmp_path / "_conflicts"
    names = [p.name for p in q.iterdir()]
    assert len(names) == 1 and names[0].startswith("projects.p (1).json.")
    assert st.read("projects/p.json")["rev"] == 1
    assert st.list_names("projects") == ["p.json"]
    lines = []
    for d in (tmp_path / "events" / "runner").iterdir():
        lines += d.read_text(encoding="utf-8").splitlines()
    ev = [json.loads(x) for x in lines]
    assert ev[0]["kind"] == "sync.conflict" and ev[0]["data"]["higher_rev"] is True and ev[0]["seq"] == 1


def test_stale_temp_files_cleaned_only_when_old(tmp_path):
    st = wstore.Store(str(tmp_path))
    d = tmp_path / "d"
    d.mkdir()
    old = d / ".a.json.runner.99999999.abcd1234.tmp"
    new = d / ".b.json.runner.99999999.abcd1234.tmp"
    old.write_text("x")
    new.write_text("x")
    t = time.time() - 3600
    os.utime(old, (t, t))
    assert st.clean_stale_temps("d") == [old.name]
    assert new.exists() and not old.exists()


# ---------------------------------------------------------------- claims

class Clock:
    def __init__(self):
        self.t = 1794052800.0  # 2026-11-07T12:00:00Z

    def __call__(self):
        return self.t


def mk(tmp_path, stale_s=60):
    c = Clock()
    return claims_mod.Claims(str(tmp_path), stale_s=stale_s, now=c), c


def req(token, owner="runner"):
    return dict(project="p", node="n1", owner=owner, location="/wt", token=token)


def test_claim_writes_valid_claim_file(tmp_path):
    cl, _ = mk(tmp_path)
    r = cl.claim(**req("tok-aaaaaaaa"))
    assert r.ok
    stored = cl.read("p", "n1")
    assert stored["schema"] == "waypoint.claim.v1"
    assert stored["heartbeat_at"] == "2026-11-07T12:00:00.000Z"
    assert stored["since"] == stored["heartbeat_at"]
    assert (tmp_path / "claims" / "n1.json").exists()
    assert not (tmp_path / "claims" / "n1.json.lock").exists()


def test_claim_race_exactly_one_wins(tmp_path):
    for i in range(10):
        cl, _c = mk(tmp_path / str(i))
        barrier = threading.Barrier(2)
        res = []

        def go(tok, owner):
            barrier.wait()
            res.append(cl.claim(**req(tok, owner)))

        ts = [threading.Thread(target=go, args=a) for a in (("tok-aaaaaaaa", "mod"), ("tok-bbbbbbbb", "runner"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sum(r.ok for r in res) == 1
        loser = next(r for r in res if not r.ok)
        assert loser.reason == "held"
        assert cl.read("p", "n1")["token"] in ("tok-aaaaaaaa", "tok-bbbbbbbb")


def test_same_token_refreshes(tmp_path):
    cl, c = mk(tmp_path)
    cl.claim(**req("tok-aaaaaaaa"))
    c.t += 10
    r = cl.claim(**req("tok-aaaaaaaa"))
    assert r.ok and r.claim["heartbeat_at"] == "2026-11-07T12:00:10.000Z"


def test_fresh_claim_not_reclaimable_stale_only_after_window(tmp_path):
    cl, c = mk(tmp_path)
    cl.claim(**req("tok-aaaaaaaa"))
    c.t += 59
    assert not cl.claim(**req("tok-bbbbbbbb")).ok
    c.t += 1
    assert not cl.claim(**req("tok-bbbbbbbb")).ok, "exactly at the window is still held"
    c.t += 1
    r = cl.claim(**req("tok-bbbbbbbb"))
    assert r.ok and cl.read("p", "n1")["token"] == "tok-bbbbbbbb"
    assert [p.name for p in (tmp_path / "claims").iterdir() if ".stale." in p.name] == ["n1.json.stale.tok-aaaaaaaa"]
    assert r.reclaimed_from["token"] == "tok-aaaaaaaa"


def test_heartbeat_keeps_claim_alive_and_checks_token(tmp_path):
    cl, c = mk(tmp_path)
    cl.claim(**req("tok-aaaaaaaa"))
    c.t += 50
    assert cl.heartbeat("p", "n1", "tok-aaaaaaaa")
    assert not cl.heartbeat("p", "n1", "tok-wrong-000")
    c.t += 50
    assert not cl.claim(**req("tok-bbbbbbbb")).ok


def test_cloud_owner_has_longer_stale_window(tmp_path):
    c = Clock()
    cl = claims_mod.Claims(str(tmp_path), now=c)
    cl.claim(**req("tok-aaaaaaaa", "cloud:run-1"))
    c.t += 5 * 60 + 1
    assert not cl.claim(**req("tok-bbbbbbbb")).ok, "cloud claim uses the 10 minute window"
    c.t += 5 * 60
    assert cl.claim(**req("tok-bbbbbbbb")).ok
    # a short configured window is never applied below the owner-kind minimum
    c2 = Clock()
    cl2 = claims_mod.Claims(str(tmp_path / "x"), stale_s=1, now=c2)
    cl2.claim(**req("tok-aaaaaaaa", "cloud:run-1"))
    c2.t += 60
    assert not cl2.claim(**req("tok-bbbbbbbb")).ok


def test_fresh_lock_respected_stale_lock_taken_over(tmp_path):
    cl, c = mk(tmp_path)
    (tmp_path / "claims").mkdir()
    lock = tmp_path / "claims" / "n1.json.lock"
    lock.write_text(json.dumps({"at": "2026-11-07T12:00:00.000Z"}))
    assert not cl.claim(**req("tok-aaaaaaaa")).ok
    c.t += 61
    assert cl.claim(**req("tok-aaaaaaaa")).ok


def test_only_token_holder_releases(tmp_path):
    cl, _ = mk(tmp_path)
    cl.claim(**req("tok-aaaaaaaa"))
    assert cl.release("p", "n1", "tok-wrong-000") is False
    assert cl.release("p", "n1", "tok-aaaaaaaa") is True
    assert cl.read("p", "n1") is None
    assert cl.claim(**req("tok-cccccccc")).ok


def test_claim_rejects_bad_shapes(tmp_path):
    cl, _ = mk(tmp_path)
    with pytest.raises(ValueError):
        cl.claim(**req("short"))
    with pytest.raises(ValueError):
        cl.claim(**req("tok-aaaaaaaa", "somebody"))
    with pytest.raises(ValueError):
        cl.claim(**{**req("tok-aaaaaaaa"), "node": "../x"})


def test_sample_claims_are_readable(tmp_path):
    root = tmp_path / "f"
    (root / "claims").mkdir(parents=True)
    for f in (SAMPLE / "claims").glob("*.json"):
        (root / "claims" / f.name).write_bytes(f.read_bytes())
    got = claims_mod.Claims(str(root)).list_claims()
    assert sorted(c["node"] for c in got) == ["cloud-01", "data-schema-ops-queue"]


# ---------------------------------------------------------------- hygiene

def test_no_third_party_imports():
    import ast
    std = set(sys.stdlib_module_names)
    files = list((PY_ROOT / "waypoint_data").glob("*.py"))
    assert files
    for f in files:
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0:
                mods = [n.module]
            for m in mods:
                assert m.split(".")[0] in std, (f.name, m)
