"""Versions end to end over real HTTP: an in-process engine on an ephemeral port, a temp database and temp
token files (never the live engine on 8751 or data/cc.sqlite). Spec: docs/v5/versions_contract.md."""
import json

import pytest

from e2e_helpers import Engine, build_alpha, node


@pytest.fixture
def eng(tmp_path):
    e = Engine(tmp_path / "engine")
    yield e
    e.close()


@pytest.fixture
def alpha(eng):
    build_alpha(eng)
    return eng


def project_ids(d):
    return [p["id"] for p in d["projects"]]


# ------------------------------------------------------------------ family, projects, history, board
def test_engine_is_isolated(eng):
    assert eng.port != 8751
    assert str(eng.db_path).startswith(str(eng.base))
    assert eng.get("/api/health")[1]["service"] == "cc-engine"


def test_projects_before_and_after_grouping(eng):
    build_alpha(eng, group=False)
    status, d = eng.get("/api/projects")
    assert status == 200 and sorted(project_ids(d)) == ["alpha-v1", "alpha-v2"]
    assert all(p["family"] is None and p["family_title"] is None and p["version"] is None and p["versions"] == 0
               for p in d["projects"])
    status, d = eng.group("alpha", "Alpha", ["alpha-v1", "alpha-v2"])
    assert status == 200 and d["ok"] and d["members"] == ["alpha-v1", "alpha-v2"]
    status, d = eng.get("/api/projects")
    assert project_ids(d) == ["alpha-v2"]  # only the current version by default
    cur = d["projects"][0]
    assert (cur["family"], cur["family_title"], cur["version"], cur["versions"]) == ("alpha", "Alpha", 2, 2)
    status, d = eng.get("/api/projects?all=1")
    by = {p["id"]: p for p in d["projects"]}
    assert sorted(by) == ["alpha-v1", "alpha-v2"]
    assert by["alpha-v1"]["archived"] in (1, True) and by["alpha-v1"]["version"] == 1
    assert by["alpha-v2"]["archived"] in (0, False)
    assert by["alpha-v1"]["family"] == "alpha" and by["alpha-v1"]["versions"] == 2


def test_family_call_is_idempotent_and_keeps_statuses(alpha):
    before = {p: alpha.status_map(p) for p in ("alpha-v1", "alpha-v2")}
    status, d = alpha.group("alpha", "Alpha", ["alpha-v1", "alpha-v2"])
    assert status == 200
    assert {p: alpha.status_map(p) for p in ("alpha-v1", "alpha-v2")} == before
    status, d = alpha.get("/api/projects?all=1")
    assert [p["version"] for p in sorted(d["projects"], key=lambda p: p["id"])] == [1, 2]
    # retitle
    assert alpha.group("alpha", "Alpha Renamed", ["alpha-v1", "alpha-v2"])[0] == 200
    assert alpha.get("/api/history?family=alpha")[1]["family"] == {"id": "alpha", "title": "Alpha Renamed"}


def test_history_by_family_and_by_project(alpha):
    status, h = alpha.get("/api/history?family=alpha")
    assert status == 200
    assert h["family"] == {"id": "alpha", "title": "Alpha"} and h["current"] == "alpha-v2"
    assert [(v["project"], v["version"], v["archived"]) for v in h["versions"]] == [
        ("alpha-v1", 1, True), ("alpha-v2", 2, False)]
    assert h["versions"][0]["counts"] == {"done": 1, "open": 4, "skipped": 0, "carried": 0}
    assert h["versions"][1]["counts"] == {"done": 1, "open": 3, "skipped": 0, "carried": 0}
    assert h["skipped"] == []
    status, h2 = alpha.get("/api/history?project=alpha-v1")  # resolves the project's family
    assert status == 200 and h2 == h


def test_history_project_without_family_and_not_found(eng):
    eng.import_plan("solo", "Solo", [node("A")])
    status, h = eng.get("/api/history?project=solo")
    assert status == 200 and h["family"]["id"] is None and h["current"] == "solo"
    assert [v["project"] for v in h["versions"]] == ["solo"]
    assert eng.get("/api/history?family=nope")[0] == 404
    assert eng.get("/api/history?project=nope")[0] == 404
    assert eng.get("/api/history")[0] == 400


def test_board_of_archived_version_is_readable(alpha):
    status, board = alpha.get("/api/board?project=alpha-v1")
    assert status == 200
    p = board["project"]
    assert (p["family"], p["family_title"], p["version"]) == ("alpha", "Alpha", 1)
    assert p["archived"] is True and p["status"] == "archived"
    assert sorted(alpha.status_map("alpha-v1")) == ["A", "B", "C", "D", "F"]
    assert board["counts"]["total"] == 5


# ------------------------------------------------------------------ roll over
def roll(eng, who="router", **kw):
    body = {"family": "alpha", "revision": eng.revision()}
    body.update(kw)
    return eng.post("/api/version/new", body, who)


def test_new_version_carry_skip_and_stale_409(alpha):
    stale_rev = alpha.revision()
    # somebody else writes first, so ``stale_rev`` is now out of date
    assert alpha.set_status("alpha-v2", "F", "needs_you")[0] == 200
    now = alpha.revision()
    assert now != stale_rev
    status, d = alpha.post("/api/version/new", {"family": "alpha", "revision": stale_rev}, "router")
    assert status == 409 and d == {"error": "stale", "revision": now}
    assert alpha.get("/api/projects")[1]["projects"][0]["id"] == "alpha-v2"  # nothing changed
    assert alpha.revision() == now

    status, d = roll(alpha, decisions={"G": {"action": "skip", "reason": "not worth it"},
                                       "F": {"action": "carry"}})
    assert status == 200, d
    assert d["project"] == "alpha-v3" and d["version"] == 3
    assert d["carried"] == ["F", "H"]  # H was undecided: carried by default
    assert d["skipped"] == [{"id": "G", "reason": "not worth it"}]
    assert d["revision"] == alpha.revision()

    # old version: closed nodes with evidence; done node untouched
    assert alpha.status_map("alpha-v2") == {"F": "carried", "G": "skipped", "H": "carried", "I": "done"}
    assert alpha.node("alpha-v2", "F")["evidence"] == "carried to alpha-v3/F"
    assert alpha.node("alpha-v2", "G")["evidence"] == "skipped: not worth it"
    # new version: copies not_started, deps only where carried, plus the discovery node
    new = alpha.statuses("alpha-v3")
    assert sorted(new) == ["F", "H", "Q1"]
    assert all(n["status"] == "not_started" and n["attempts"] == 0 for n in new.values())
    assert new["H"]["deps"] == ["F"] and new["F"]["deps"] == []
    assert new["Q1"]["lane"] == "discovery"
    q1 = alpha.node("alpha-v3", "Q1")
    assert q1["title"] == "Discovery questions for Alpha v3"
    assert q1["check"] == {"type": "human",
                           "prompt": "Discovery questions for Alpha v3 answered (waypoint-planning)"}
    # projects: only v3 is current now
    status, projs = alpha.get("/api/projects")
    assert project_ids(projs) == ["alpha-v3"]
    assert projs["projects"][0]["versions"] == 3 and projs["projects"][0]["version"] == 3

    # History: skipped idea keeps its goal, v2 is archived with counts
    status, h = alpha.get("/api/history?family=alpha")
    assert h["current"] == "alpha-v3"
    assert [v["project"] for v in h["versions"]] == ["alpha-v1", "alpha-v2", "alpha-v3"]
    v2 = h["versions"][1]
    assert v2["archived"] is True and v2["counts"] == {"done": 1, "open": 0, "skipped": 1, "carried": 2}
    assert h["versions"][2]["counts"] == {"done": 0, "open": 3, "skipped": 0, "carried": 0}
    assert len(h["skipped"]) == 1
    s = h["skipped"][0]
    assert (s["project"], s["version"], s["id"], s["title"], s["goal"], s["reason"]) == (
        "alpha-v2", 2, "G", "Task G", "Goal of G.", "not worth it")
    assert s["at"]

    # one event per decision plus the summary, each decision carrying the revision
    versions_events = alpha.events("alpha-v2", "version") + alpha.events("alpha-v3", "version")
    per_node = [e for e in versions_events if e.get("node_id")]
    assert {e["node_id"] for e in per_node} == {"F", "G", "H"}
    assert all(e["data"]["revision"] == d["revision"] for e in per_node)
    assert any(e["project_id"] == "alpha-v3" and not e.get("node_id") for e in versions_events)


def test_new_version_options_title_id_and_defaults(alpha):
    status, d = roll(alpha, title="Alpha Next", id="alpha-next")
    assert status == 200 and d["project"] == "alpha-next"
    assert alpha.get("/api/board?project=alpha-next")[1]["project"]["title"] == "Alpha Next"
    assert alpha.node("alpha-next", "Q1")["title"] == "Discovery questions for Alpha Next"
    status, d = roll(alpha, id="alpha-next")  # id already taken: made unique
    assert status == 200 and d["project"] == "alpha-next-2" and d["version"] == 4


def test_new_version_bad_decisions_are_400_and_write_nothing(alpha):
    rev = alpha.revision()
    for decisions in ({"G": {"action": "skip"}},                         # no reason
                      {"G": {"action": "skip", "reason": "   "}},
                      {"I": {"action": "carry"}},                         # done: not unfinished
                      {"ZZ": {"action": "carry"}},                        # unknown task
                      {"G": {"action": "maybe"}},
                      "carry everything"):
        status, d = alpha.post("/api/version/new", {"family": "alpha", "revision": rev, "decisions": decisions})
        assert status == 400, (decisions, d)
    assert alpha.revision() == rev
    assert project_ids(alpha.get("/api/projects")[1]) == ["alpha-v2"]
    assert alpha.post("/api/version/new", {"family": "alpha"})[0] == 400  # revision is required
    assert alpha.post("/api/version/new", {"family": "nope", "revision": rev})[0] == 404
    assert alpha.post("/api/version/new", {"revision": rev})[0] == 400  # neither family nor project


def test_new_version_from_a_project_without_family_creates_one(eng):
    eng.import_plan("gamma-v3", "Gamma", [node("A"), node("B", ["A"])])
    status, d = eng.post("/api/version/new", {"project": "gamma-v3", "revision": eng.revision()}, "owner")
    assert status == 200, d
    assert d["project"] == "gamma-v2" and d["version"] == 2  # family id strips -v3; the source becomes version 1
    h = eng.get("/api/history?project=gamma-v3")[1]
    assert h["family"] == {"id": "gamma", "title": "Gamma"} and h["current"] == "gamma-v2"
    assert [v["archived"] for v in h["versions"]] == [True, False]


def test_actor_comes_from_the_credential(alpha):
    status, d = roll(alpha, who="router")
    assert status == 200
    ev = [e for e in alpha.events(d["project"], "version") if not e.get("node_id")][0]
    assert ev["actor"] == "router" and "Router" in ev["summary"]
    status, d2 = roll(alpha, who="owner")
    assert status == 200
    ev = [e for e in alpha.events(d2["project"], "version") if not e.get("node_id")][0]
    assert ev["actor"] == "owner" and "Router" not in ev["summary"]


# ------------------------------------------------------------------ decide
def test_decide_from_archived_version_with_id_collision(alpha):
    status, d = roll(alpha, decisions={"G": {"action": "skip", "reason": "later"}})
    assert status == 200 and d["project"] == "alpha-v3"
    assert sorted(alpha.status_map("alpha-v3")) == ["F", "H", "Q1"]  # v3 already holds an F

    status, d = alpha.post("/api/version/decide", {
        "project": "alpha-v1", "revision": alpha.revision(),
        "decisions": {"B": {"action": "carry"}, "C": {"action": "carry"}, "F": {"action": "carry"},
                      "D": {"action": "skip", "reason": "dropped in review"}}}, "router")
    assert status == 200, d
    assert sorted(d["carried"], key=lambda m: m["from"]) == [
        {"from": "B", "to": "B"}, {"from": "C", "to": "C"}, {"from": "F", "to": "v1-F"}]
    assert d["skipped"] == [{"id": "D", "reason": "dropped in review"}]
    assert d["revision"] == alpha.revision()

    assert alpha.status_map("alpha-v1") == {"A": "done", "B": "carried", "C": "carried", "D": "skipped",
                                            "F": "carried"}
    assert alpha.node("alpha-v1", "F")["evidence"] == "carried to alpha-v3/v1-F"
    assert alpha.node("alpha-v1", "D")["evidence"] == "skipped: dropped in review"
    new = alpha.statuses("alpha-v3")
    assert sorted(new) == ["B", "C", "F", "H", "Q1", "v1-F"]
    assert new["C"]["deps"] == ["B"] and new["B"]["deps"] == []  # A was done, not carried: dep dropped
    assert new["v1-F"]["status"] == "not_started" and new["F"]["status"] == "not_started"
    # the skipped idea appears in History with its goal
    h = alpha.get("/api/history?family=alpha")[1]
    assert [(s["project"], s["id"], s["reason"], s["goal"]) for s in h["skipped"] if s["project"] == "alpha-v1"] == [
        ("alpha-v1", "D", "dropped in review", "Goal of D.")]
    assert h["versions"][0]["counts"] == {"done": 1, "open": 0, "skipped": 1, "carried": 3}


def test_decide_refusals(alpha):
    rev = alpha.revision()
    body = {"project": "alpha-v2", "revision": rev, "decisions": {"G": {"action": "carry"}}}
    status, d = alpha.post("/api/version/decide", body)  # the current version
    assert status == 400 and "current version" in d["detail"]
    for decisions in ({"A": {"action": "carry"}},       # done
                      {}, {"B": {"action": "skip"}}):    # nothing decided / no reason
        status, d = alpha.post("/api/version/decide", {"project": "alpha-v1", "revision": rev,
                                                       "decisions": decisions})
        assert status == 400, (decisions, d)
    assert alpha.revision() == rev
    # stale
    alpha.set_status("alpha-v1", "B", "needs_you")
    status, d = alpha.post("/api/version/decide", {"project": "alpha-v1", "revision": rev,
                                                   "decisions": {"B": {"action": "carry"}}})
    assert status == 409 and d["error"] == "stale" and d["revision"] == alpha.revision()
    assert alpha.post("/api/version/decide", {"project": "nope", "revision": rev,
                                              "decisions": {"B": {"action": "carry"}}})[0] == 404


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("who", ["router", "owner", "origin"])
@pytest.mark.parametrize("target", ["skipped", "carried"])
def test_router_setting_skipped_or_carried_is_400_for_every_actor(alpha, who, target):
    rev = alpha.revision()
    status, d = alpha.post("/api/node/status", {"project": "alpha-v2", "id": "G", "status": target,
                                                "evidence": "no", "revision": rev}, who)
    assert status == 400 and d["error"] == "bad_request" and "version roll-over" in d["detail"]
    assert alpha.revision() == rev and alpha.status_map("alpha-v2")["G"] == "not_started"


def test_rolling_over_a_non_current_member_is_400(alpha):
    rev = alpha.revision()
    status, d = alpha.post("/api/version/new", {"project": "alpha-v1", "revision": rev})
    assert status == 400 and "only the current version (alpha-v2) can roll over" in d["detail"]
    assert alpha.revision() == rev and project_ids(alpha.get("/api/projects?all=1")[1]) == ["alpha-v1", "alpha-v2"]
    # rolling the current one by project id works
    status, d = alpha.post("/api/version/new", {"project": "alpha-v2", "revision": rev})
    assert status == 200 and d["project"] == "alpha-v3"


def test_set_family_cannot_steal_a_member_of_another_family(alpha):
    alpha.import_plan("other-a", "Other A", [node("A")])
    alpha.import_plan("other-b", "Other B", [node("A")])
    assert alpha.group("other", "Other", ["other-a", "other-b"])[0] == 200
    rev = alpha.revision()
    # alpha-v1 belongs to 'alpha' whose alpha-v2 stays behind
    status, d = alpha.post("/api/version/family", {"family": "other", "title": "Other",
                                                   "members": ["alpha-v1", "other-a", "other-b"],
                                                   "revision": rev}, "owner")
    assert status == 400 and "alpha-v1" in d["detail"] and "alpha-v2" in d["detail"]
    # taking one member of 'other' for a new family would leave 'other' with a hole
    status, d = alpha.post("/api/version/family", {"family": "thief", "title": "Thief",
                                                   "members": ["other-a"], "revision": rev}, "owner")
    assert status == 400 and "other-a" in d["detail"]
    assert alpha.revision() == rev
    assert alpha.get("/api/history?family=thief")[0] == 404
    h = alpha.get("/api/history?family=other")[1]
    assert [v["project"] for v in h["versions"]] == ["other-a", "other-b"] and h["current"] == "other-b"
    h = alpha.get("/api/history?family=alpha")[1]
    assert [v["project"] for v in h["versions"]] == ["alpha-v1", "alpha-v2"]
    # moving a whole family is allowed, and the emptied family goes
    status, d = alpha.post("/api/version/family", {"family": "merged", "title": "Merged",
                                                   "members": ["other-a", "other-b"], "revision": rev}, "owner")
    assert status == 200
    assert alpha.get("/api/history?family=other")[0] == 404
    assert [v["project"] for v in alpha.get("/api/history?family=merged")[1]["versions"]] == ["other-a", "other-b"]


def test_set_family_input_validation(alpha):
    rev = alpha.revision()
    for body in ({"family": "bad id!", "title": "T", "members": ["alpha-v1"]},
                 {"family": "x", "title": " ", "members": ["alpha-v1"]},
                 {"family": "x", "title": "T", "members": ["alpha-v1", "alpha-v1"]},
                 {"family": "x", "title": "T", "members": "alpha-v1"}):
        status, d = alpha.post("/api/version/family", dict(body, revision=rev), "owner")
        assert status == 400, (body, d)
    assert alpha.post("/api/version/family", {"family": "x", "title": "T", "members": ["ghost"],
                                              "revision": rev}, "owner")[0] == 404
    assert alpha.revision() == rev


# ------------------------------------------------------------------ cloud sync
def test_cloud_sync_against_a_carried_node_is_a_conflict_and_changes_nothing(alpha, tmp_path):
    status, d = roll(alpha, decisions={"G": {"action": "skip", "reason": "later"}})
    assert status == 200
    assert alpha.status_map("alpha-v2")["F"] == "carried"
    before = {p: alpha.status_map(p) for p in ("alpha-v1", "alpha-v2", "alpha-v3")}
    before_nodes = {p: {k: (v["status"], v["actor"], v["attempts"]) for k, v in alpha.statuses(p).items()}
                    for p in before}
    repo = tmp_path / "repo"
    (repo / "sync" / "inbox").mkdir(parents=True)
    rev = alpha.revision()
    doc = {"schema": "waypoint.sync.v1", "session": "cloud-1", "written_at": "2026-11-05T10:00:00Z", "items": [
        {"kind": "node_status", "project": "alpha-v2", "id": "F", "status": "running", "evidence": "x",
         "base_revision": rev},                                   # carried
        {"kind": "node_status", "project": "alpha-v2", "id": "G", "status": "running", "evidence": "x",
         "base_revision": rev},                                   # skipped
        {"kind": "node_status", "project": "alpha-v1", "id": "B", "status": "running", "evidence": "x",
         "base_revision": rev}]}                                  # open node, but of an archived version
    (repo / "sync" / "inbox" / "20261105-cloud_1-1.json").write_text(json.dumps(doc), encoding="utf-8")

    status, res = alpha.post("/api/sync/import", {"repo": str(repo)}, "router")
    assert status == 403  # owner only
    status, res = alpha.post("/api/sync/import", {"repo": str(repo)}, "owner")
    assert status == 200, res
    assert (res["files"], res["applied"], res["conflicts"]) == (1, 0, 3)

    for p in before:
        assert alpha.status_map(p) == before[p]
    after_nodes = {p: {k: (v["status"], v["actor"], v["attempts"]) for k, v in alpha.statuses(p).items()}
                   for p in before}
    assert after_nodes == before_nodes
    conflicts = json.loads((repo / "sync" / "conflicts" / "20261105-cloud_1-1.json").read_text())["conflicts"]
    reasons = [c["reason"] for c in conflicts]
    assert any("alpha-v2/F is carried" in r for r in reasons)
    assert any("alpha-v2/G is skipped" in r for r in reasons)
    assert any("alpha-v1 is archived" in r for r in reasons)
    assert alpha.events("alpha-v2", "sync_conflict") and (repo / "sync" / "processed").exists()
    assert not list((repo / "sync" / "inbox").iterdir())
    # the carried copy in the current version is still a normal, writable node
    assert alpha.set_status("alpha-v3", "F", "needs_you")[0] == 200


# ------------------------------------------------------------------ credentials
def test_owner_only_routes_owner_token_vs_router_token(alpha):
    def fam(who):
        return alpha.post("/api/version/family", {"family": "alpha", "title": "Alpha",
                                                  "members": ["alpha-v1", "alpha-v2"],
                                                  "revision": alpha.revision()}, who)
    status, d = fam("router")
    assert status == 403 and d["error"] == "owner_only"
    assert fam("owner")[0] == 200
    assert fam("origin")[0] == 200
    status, d = fam("none")
    assert status == 403 and d["error"] == "forbidden"
    status, d = fam("bad-owner")
    assert status == 403 and "owner token does not match" in d["detail"]
    assert fam("router-as-owner")[0] == 403   # the router token is not the owner token
    assert fam("owner-as-router")[0] == 403   # and the owner token is not the router token


def test_routes_open_to_owner_or_router_refuse_no_credential(alpha):
    for who in ("none", "bad-owner", "router-as-owner", "owner-as-router"):
        rev = alpha.revision()
        assert alpha.post("/api/version/new", {"family": "alpha", "revision": rev}, who)[0] == 403
        assert alpha.post("/api/version/decide", {"project": "alpha-v1", "revision": rev,
                                                  "decisions": {"B": {"action": "carry"}}}, who)[0] == 403
        assert alpha.revision() == rev
    for who, nid in (("router", "B"), ("owner", "C")):  # both may call decide / new
        assert alpha.post("/api/version/decide", {"project": "alpha-v1", "revision": alpha.revision(),
                                                  "decisions": {nid: {"action": "skip", "reason": who}}},
                          who)[0] == 200


def test_owner_token_header_wins_over_router_token(alpha):
    # both headers present: the owner header is checked first, so a wrong owner token is refused even
    # though the router token is right
    req_headers = {**alpha.headers("router"), "X-CC-Owner-Token": "wrong"}
    import urllib.error
    import urllib.request
    req = urllib.request.Request(alpha.url + "/api/version/new", method="POST",
                                 data=json.dumps({"family": "alpha", "revision": alpha.revision()}).encode(),
                                 headers={**req_headers, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403
    # a router-imported plan lands as draft; only the owner credential may accept it
    alpha.post("/api/plan/import", {"plan": {"id": "drafty", "title": "Drafty", "brief": "b", "cwd": None,
                                             "nodes": [node("A")]}, "revision": alpha.revision()}, "router")
    assert alpha.post("/api/plan/accept", {"project": "drafty"}, "router")[0] == 403
    assert alpha.post("/api/plan/accept", {"project": "drafty"}, "owner")[0] == 200
