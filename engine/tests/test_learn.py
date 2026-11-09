import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_engine import learn as L  # noqa: E402


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


def _c(cid, prereqs=(), group="behavioural"):
    return {"id": cid, "title": cid.upper(), "group": group, "prereqs": list(prereqs),
            "explain": f"About {cid}.", "evidence": []}


def _q(qid, comps, stories=("s",), **kw):
    d = {"id": qid, "text": f"{qid}?", "competencies": list(comps), "stories": list(stories), "roles": ["r1", "r2"]}
    d.update(kw)
    return d


STORY = {"id": "s", "title": "S", "situation": "sit", "task": "task", "action": "act", "result": "res",
         "pivot": "piv", "link": "lnk", "sixty": "sixty words", "say_it": {"avoid": "x", "say": "y"},
         "limits": "lim", "evidence": ["e.md"], "competencies": ["a"], "roles": ["r1"]}

SMALL = {
    "schema_version": 2,
    "roles": [{"id": "r1", "label": "Risk", "match": ["risk", "acme"], "evidence": []},
              {"id": "r2", "label": "Consulting", "match": ["consulting", "bain"], "evidence": []}],
    "competencies": [_c("a"), _c("b", ["a"]), _c("c", ["a", "b"]), _c("d")],
    "stories": [STORY],
    "questions": [_q("qa1", ["a"]), _q("qa2", ["a", "b"]), _q("qb", ["b"]), _q("qc", ["c"]),
                  _q("qd", ["d"], stories=(), outline=["one", "two"])],
}


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def lr(tmp_path, clock):
    return L.Learn(db_path=tmp_path / "learn.sqlite", seed=SMALL, now=clock, company_dir=tmp_path / "none")


# ---- practice loop
def test_next_step_starts_at_a_root_with_a_question_and_hidden_model_answer(lr):
    step = lr.next_step()
    assert step["competency"] == "a" and step["concept"] == "a"
    assert step["question"]["id"] == "qa1" and step["check_question"] == "qa1?"
    ma = step["model_answer"]
    assert [b["label"] for b in ma["bullets"]] == ["Situation", "Turning point", "Action", "Result", "Link"]
    assert ma["sixty"] == "sixty words" and ma["story"]["id"] == "s"
    assert step["intervals"] == {"again": "10 min", "hard": "1 day", "good": "1 day", "easy": "4 days"}
    assert step["builds_on"] == [] and "b" in step["unlocks"]


def test_next_step_respects_prerequisites_until_everything_is_mastered(lr, clock):
    seen = []
    for _ in range(60):
        step = lr.next_step()
        if step is None:
            break
        m = lr.mastery()
        assert all(m[p] >= 3 for p in lr.by_id[step["competency"]]["prereqs"]), (step, m)
        seen.append(step["competency"])
        lr.record(step["question"]["id"], "good")
    assert lr.next_step() is None
    assert seen.index("b") > seen.index("a") and seen.index("c") > seen.index("b")


def test_weakest_unlocked_competency_wins(lr):
    for _ in range(3):
        lr.record("qa1", 3)
    lr.record("d", "set:2")
    # candidates: a(3), b(0, now unlocked), d(2) -> b
    assert lr.next_step()["competency"] == "b"


def test_rating_moves_mastery_and_schedules_the_card(lr, clock):
    r = lr.record("qa1", "good")
    assert r["before"] == 0 and r["mastery"] == 1 and r["status"] == "learning"
    assert r["card"]["interval"] == 1.0 and r["next_review"] == "1 day"
    r = lr.record("qa1", 4)
    assert r["mastery"] == 3 and r["card"]["reps"] == 2
    r = lr.record("qa1", "again")
    assert r["mastery"] == 2 and r["card"]["reps"] == 0 and r["next_review"] == "10 min"
    r = lr.record("qa1", "hard")
    assert r["mastery"] == 2


def test_unseen_question_comes_before_a_reviewed_one_and_due_cards_return(lr, clock):
    lr.record("d", "set:4")           # park the other root so competency a stays the weakest
    lr.record("qa1", "good")          # qa1 is now due tomorrow; qa2 (also primary for a) is unseen
    step = lr.next_step()
    assert step["competency"] == "a" and step["question"]["id"] == "qa2"
    lr.record("qa2", "good")
    clock.advance(days=2)
    step = lr.next_step()
    assert step["question"]["id"] == "qa1" and step["card"]["due"] is True


def test_due_cards_are_still_offered_once_mastered(lr, clock):
    for qid in ("qa1", "qa1", "qa2", "qb", "qb", "qc", "qc", "qd", "qd"):
        lr.record(qid, "easy")
    assert all(v == 4 for v in lr.mastery().values())
    assert lr.next_step() is None
    clock.advance(days=30)
    step = lr.next_step()
    assert step is not None and step["card"]["due"]


def test_schedule_card_sm2_lite_intervals():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    c = L.schedule_card({}, 3, now)
    assert c["interval"] == 1.0
    c = L.schedule_card(c, 3, now)
    assert c["interval"] == 3.0
    c = L.schedule_card(c, 3, now)
    assert c["interval"] == round(3.0 * 2.5, 2)
    e = L.schedule_card({}, 4, now)
    assert e["interval"] == 4.0 and e["ease"] > 2.5
    h = L.schedule_card({"reps": 2, "ease": 2.5, "interval": 10}, 2, now)
    assert h["interval"] == 12.0 and h["ease"] < 2.5
    a = L.schedule_card({"reps": 5, "ease": 1.35, "interval": 40}, 1, now)
    assert a["reps"] == 0 and a["ease"] == 1.3
    assert L._parse(a["due_at"]) - now == timedelta(minutes=10)
    with pytest.raises(ValueError):
        L.schedule_card({}, 5, now)


def test_record_outcome_forms_and_errors(lr):
    assert lr.record("qa1", True)["outcome"] == "good"
    assert lr.record("qa1", "incorrect")["outcome"] == "again"
    assert lr.record("qa1", "2")["outcome"] == "hard"
    assert lr.record("a", {"set": 4})["mastery"] == 4
    assert lr.record("a", "skip")["mastery"] == 4
    assert lr.record("d", "known")["mastery"] == 4
    with pytest.raises(KeyError):
        lr.record("ghost", "good")
    with pytest.raises(ValueError):
        lr.record("qa1", "bogus")
    with pytest.raises(ValueError):
        lr.record("a", "set:9")
    with pytest.raises(ValueError):
        lr.record("qa1", 7)


def test_mastery_and_cards_persist(lr, tmp_path, clock):
    lr.record("qa1", "good")
    again = L.Learn(db_path=tmp_path / "learn.sqlite", seed=SMALL, now=clock)
    assert again.mastery()["a"] == 1
    assert again._cards()["qa1"]["reps"] == 1


def test_outline_question_model_answer(lr):
    step = lr.question_step("qd")
    assert step["mode"] == "chosen"
    assert [b["text"] for b in step["model_answer"]["bullets"]] == ["one", "two"]
    assert step["model_answer"]["story"] is None
    with pytest.raises(KeyError):
        lr.question_step("nope")


def test_concept_graph_counts_edges_and_due(lr):
    g = lr.concept_graph()
    by = {n["id"]: n for n in g["nodes"]}
    assert by["a"]["status"] == "ready" and by["b"]["status"] == "not_started"
    assert ["a", "c"] not in g["edges"] and ["b", "c"] in g["edges"]   # a->c implied by a->b->c
    assert by["a"]["questions"] == ["qa1", "qa2"] and by["b"]["questions"] == ["qa2", "qb"]
    for _ in range(3):
        lr.record("qa1", "good")
    g = lr.concept_graph()
    assert {n["id"]: n["status"] for n in g["nodes"]}["b"] == "ready"
    assert g["counts"]["mastered"] == 1


def test_snapshot_keeps_question_list_light(lr):
    snap = lr.snapshot()
    assert snap["total"] == 4 and len(snap["stories"]) == 1 and len(snap["questions"]) == 5
    assert "model_answer" not in snap["questions"][0]
    assert snap["stories"][0]["questions"] == ["qa1", "qa2", "qb", "qc"]
    assert snap["next"]["question"]["id"] == "qa1"


# ---- before an interview
def test_prep_matches_role_and_company_file(tmp_path, clock):
    cdir = tmp_path / "Company Research"
    cdir.mkdir()
    (cdir / "Acme - Analyst Audit.md").write_text("x", encoding="utf-8")
    (cdir / "Bain & Company - Consultant Audit.md").write_text("x", encoding="utf-8")
    seed = json.loads(json.dumps(SMALL))
    seed["questions"].append(_q("qr", ["d"], roles=["r1"], weight=1))
    seed["questions"].append(_q("qx", ["d"], roles=["r2"], weight=1))
    lr = L.Learn(db_path=tmp_path / "p.sqlite", seed=seed, now=clock, company_dir=cdir)
    p = lr.prep("Acme risk analyst")
    assert [r["id"] for r in p["roles"]] == ["r1"]
    assert [Path(f).name for f in p["company_files"]] == ["Acme - Analyst Audit.md"]
    ids = [q["id"] for q in p["questions"]]
    assert "qr" in ids and "qx" not in ids
    assert all("model_answer" in q for q in p["questions"])
    assert p["stories"] and p["stories"][0]["id"] == "s"


def test_prep_without_match_still_returns_questions(lr):
    p = lr.prep("somewhere")
    assert p["roles"] == [] and p["questions"] and "No role family matched" in p["note"]


# ---- seed validation
def test_seed_validation_rejects_bad_seeds():
    bad_cycle = json.loads(json.dumps(SMALL))
    bad_cycle["competencies"][0]["prereqs"] = ["c"]
    with pytest.raises(L.SeedError):
        L.validate_seed(bad_cycle)
    long = json.loads(json.dumps(SMALL))
    long["competencies"][0]["explain"] = "word " * 121
    with pytest.raises(L.SeedError):
        L.validate_seed(long)
    ghost = json.loads(json.dumps(SMALL))
    ghost["competencies"][1]["prereqs"] = ["ghost"]
    with pytest.raises(L.SeedError):
        L.validate_seed(ghost)
    orphan = json.loads(json.dumps(SMALL))
    orphan["questions"] = [q for q in orphan["questions"] if q["id"] != "qd"]
    with pytest.raises(L.SeedError, match="every competency"):
        L.validate_seed(orphan)
    no_answer = json.loads(json.dumps(SMALL))
    no_answer["questions"][-1].pop("outline")
    with pytest.raises(L.SeedError, match="model answer"):
        L.validate_seed(no_answer)


def test_db_path_injectable(tmp_path):
    db = tmp_path / "sub" / "x.sqlite"
    first = L.next_step(db_path=db)
    L.record(first["question"]["id"], "good", db_path=db)
    assert db.exists()
    assert L.concept_graph(db_path=db)["nodes"][0]["mastery"] == 1


# ---- extension routes
class FakeH:
    def __init__(self, body=None):
        self._body = body

    def _json_body(self):
        return self._body


def test_routes_are_declared_and_work(tmp_path, monkeypatch):
    inst = L.Learn(db_path=tmp_path / "r.sqlite")
    monkeypatch.setattr(L, "_default", inst)
    assert set(L.ROUTES) == {"GET /api/interview", "GET /api/interview/prep", "GET /api/interview/question",
                             "POST /api/interview/rate", "GET /api/learn/live_prompt",
                             "GET /api/interview/live_prompt"}
    snap = L.ROUTES["GET /api/interview"](FakeH(), {})
    qid = snap["next"]["question"]["id"]
    assert L.ROUTES["GET /api/interview/question"](FakeH(), {"id": qid})["question"]["id"] == qid
    res = L.ROUTES["POST /api/interview/rate"](FakeH({"question": qid, "rating": 3}), {})
    assert res["ok"] and res["result"]["mastery"] == 1
    prep = L.ROUTES["GET /api/interview/prep"](FakeH(), {"company": "Acme", "role": "RQA analyst"})
    assert len(prep["questions"]) == L.PREP_QUESTIONS
    with pytest.raises(ValueError):
        L.ROUTES["GET /api/interview/prep"](FakeH(), {})
    with pytest.raises(ValueError):
        L.ROUTES["POST /api/interview/rate"](FakeH({"question": qid}), {})


# ---- the real seed
SEED = L.load_seed()


def test_real_seed_shape():
    assert SEED["schema_version"] == 2 and SEED["title"] == "Interview"
    comps, stories, questions = SEED["competencies"], SEED["stories"], SEED["questions"]
    assert 12 <= len(comps) <= 30
    assert 8 <= len(stories) <= 12
    assert 40 <= len(questions) <= 60
    groups = {c["group"] for c in comps}
    assert {"motivation", "deep_dive", "technical", "behavioural", "close"} <= groups
    ids = {c["id"] for c in comps}
    for c in comps:
        assert len(c["explain"].split()) <= 120 and c["evidence"], c["id"]
    for s in stories:
        for k in ("situation", "task", "action", "result", "pivot", "sixty", "limits"):
            assert s[k].strip(), (s["id"], k)
        assert s["say_it"]["avoid"] and s["say_it"]["say"]
        assert s["evidence"] and set(s["competencies"]) <= ids and s["competencies"]
        assert len(s["sixty"].split()) <= 160, s["id"]
    used = {sid for q in questions for sid in q.get("stories", [])}
    assert used == {s["id"] for s in stories}, "every story answers at least one question"


def test_real_seed_cites_the_canonical_evidence_matrix():
    matrix = [p for p in SEED["sources"] if p.endswith("Master_Evidence_Matrix.yaml")]
    assert matrix
    career = [s for s in SEED["stories"] if not s["id"].startswith("ai-")]
    assert all(any(p.endswith("Master_Evidence_Matrix.yaml") or p.endswith("README.md") for p in s["evidence"])
               for s in career)


@pytest.mark.skipif(not Path(r"~\Notes\notes").is_dir(), reason="real Drive not present")
def test_real_seed_evidence_paths_exist():
    items = SEED["competencies"] + SEED["stories"] + SEED["roles"]
    missing = [p for item in items for p in item["evidence"] if not os.path.exists(p)]
    missing += [p for p in SEED["sources"] if not os.path.exists(p)]
    assert missing == []


def test_real_seed_first_step_is_a_root(tmp_path):
    lr = L.Learn(db_path=tmp_path / "l.sqlite")
    step = lr.next_step()
    assert lr.by_id[step["competency"]]["prereqs"] == []
    assert step["model_answer"]["bullets"]


def test_real_seed_prep_for_acme_risk(tmp_path):
    lr = L.Learn(db_path=tmp_path / "l.sqlite")
    p = lr.prep("Acme RQA analyst")
    ids = [q["id"] for q in p["questions"]]
    assert len(ids) == 10 and "q-pitch-tmay" in ids and "q-ask-them" in ids and "q-why-risk" in ids
    assert sum(1 for q in p["questions"] if lr.by_id[q["competencies"][0]]["group"] == "behavioural") >= 3
    assert p["roles"][0]["id"] == "risk_quant" and 1 <= len(p["stories"]) <= 6
