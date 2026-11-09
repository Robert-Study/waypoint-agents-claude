"""Interview: prepare Owner for the interviews he is applying for, one small step at a time.

The seed (``learn_seed.json``, schema 2, hand-edited next to this file) holds:
  * ``competencies`` - what interviewers probe, as a prerequisite DAG (mastery 0-4 each);
  * ``stories``      - STAR stories built only from real evidence files, with a 60-second version;
  * ``questions``    - likely interview questions, each linked to competencies (primary first)
                       and to the stories that answer it best;
  * ``roles``        - the role families Owner targets, with match words and company audits.

Practice loop: ``next_step()`` picks the weakest competency whose prerequisites are all >= 3,
shows one of its questions (due cards first, then unseen ones), Owner answers out loud,
reveals the model outline and self-rates 1-4 (Again/Hard/Good/Easy). Each question is a
spaced-repetition card (SM-2 lite); each rating also moves the competency's mastery.
``prep(text)`` is "before an interview": the 10 most relevant questions and stories for a
free-text company/role. ``live_prompt(company, role)`` turns that list into a spoken
mock-interviewer prompt Owner copies into Gemini Live (the Gemini app, not the Gemini API).

Ratings, cards and mastery live in SQLite (``data/learn.sqlite`` by default; path injectable).
Python standard library only.

Status for the Map view uses the board status vocabulary:
  mastered (mastery >= 3) -> done; learning -> running; ready (prereqs met) -> ready;
  untouched (prereqs not met) -> not_started.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import config

SEED_PATH = Path(__file__).with_name("learn_seed.json")
SCHEMA_VERSION = 2
MAX_MASTERY = 4
UNLOCK_AT = 3
EXPLAIN_MAX_WORDS = 120
PREP_QUESTIONS = 10
PREP_STORIES = 6
PREP_PER_COMPETENCY = 2
PREP_BEHAVIOURAL = 3
PREP_GROUP_CAP = {"technical": 3, "deep_dive": 2, "motivation": 3, "foundation": 1}
# Display order for a prep list: how an interview usually runs.
GROUP_ORDER = {"motivation": 0, "foundation": 1, "deep_dive": 2, "behavioural": 3, "technical": 4, "close": 5}

LEARN_TO_BOARD = {"mastered": "done", "ready": "ready", "learning": "running", "untouched": "not_started"}

RATING_NAMES = {1: "again", 2: "hard", 3: "good", 4: "easy"}
RATING_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
RATING_WORDS = {
    "again": 1, "hard": 2, "good": 3, "easy": 4,
    # legacy outcomes from the old Learn surface
    "correct": 3, "right": 3, "pass": 3, "passed": 3, "yes": 3, "got_it": 3, "true": 3,
    "incorrect": 1, "wrong": 1, "fail": 1, "failed": 1, "no": 1, "missed": 1, "false": 1,
}
NEUTRAL = {"skip", "skipped", "later"}
KNOWN = {"known", "already_know", "mastered"}
MASTERY_DELTA = {1: -1, 2: 0, 3: 1, 4: 2}
AGAIN_MINUTES = 10
START_EASE, MIN_EASE = 2.5, 1.3

STOP = {"a", "an", "and", "the", "of", "for", "to", "in", "at", "on", "with", "or", "role", "graduate",
        "programme", "program", "2026", "2027", "2028", "uk", "london", "emea", "full", "time", "analyst",
        "scheme", "job", "interview", "position", "intern", "internship", "new", "grad", "&", "-"}


class SeedError(ValueError):
    pass


def data_dir() -> Path:
    return config.data_dir()


def default_db_path() -> Path:
    return data_dir() / "learn.sqlite"


def default_company_dir() -> Path:
    root = config.DRIVE_ROOT
    return Path(root) / config.EXCHANGE_DIR / "02 Applications & Documents" / "Company Research"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _words(text) -> list:
    return [w for w in re.findall(r"[a-z0-9&]+", str(text or "").lower()) if w]


# ------------------------------------------------------------------ seed
def resolve_drive_path(value) -> str:
    """A seed path relative to the drive root becomes absolute under ``config.DRIVE_ROOT``; absolute paths stay."""
    text = str(value)
    if os.path.isabs(text) or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith(("\\\\", "/")):
        return text
    root = config.DRIVE_ROOT
    sep = "\\" if "\\" in root else "/"
    return root.rstrip("\\/") + sep + text.replace("/", sep).replace("\\", sep)


def _resolve_seed_paths(seed: dict) -> None:
    if isinstance(seed.get("sources"), list):
        seed["sources"] = [resolve_drive_path(p) for p in seed["sources"]]
    for key in ("competencies", "stories", "roles"):
        for item in seed.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("evidence"), list):
                item["evidence"] = [resolve_drive_path(p) for p in item["evidence"]]


def load_seed(path=None) -> dict:
    p = Path(path) if path else Path(config.LEARN_SEED)
    with open(p, encoding="utf-8-sig") as fh:
        seed = json.load(fh)
    _resolve_seed_paths(seed)
    validate_seed(seed)
    return seed


def validate_seed(seed: dict) -> None:
    comps = seed.get("competencies")
    if not isinstance(comps, list) or not comps:
        raise SeedError("seed has no competencies")
    ids = [c.get("id") for c in comps]
    if len(set(ids)) != len(ids) or not all(ids):
        raise SeedError("competency ids must be unique and non-empty")
    known = set(ids)
    for c in comps:
        if not str(c.get("title", "")).strip():
            raise SeedError(f"{c['id']}: needs a title")
        for p in c.get("prereqs", []):
            if p not in known:
                raise SeedError(f"{c['id']}: unknown prerequisite {p}")
        if len(str(c.get("explain", "")).split()) > EXPLAIN_MAX_WORDS:
            raise SeedError(f"{c['id']}: explain is over {EXPLAIN_MAX_WORDS} words")
    # DAG: Kahn's algorithm must consume every competency.
    indeg = {c["id"]: len(c.get("prereqs", [])) for c in comps}
    dependants = {i: [] for i in ids}
    for c in comps:
        for p in c.get("prereqs", []):
            dependants[p].append(c["id"])
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in dependants[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(ids):
        raise SeedError("competency prerequisites contain a cycle")

    stories = seed.get("stories") or []
    sids = [s.get("id") for s in stories]
    if len(set(sids)) != len(sids) or not all(sids):
        raise SeedError("story ids must be unique and non-empty")
    for s in stories:
        for k in ("situation", "task", "action", "result"):
            if not str(s.get(k, "")).strip():
                raise SeedError(f"story {s['id']}: missing {k}")
        for c in s.get("competencies", []):
            if c not in known:
                raise SeedError(f"story {s['id']}: unknown competency {c}")

    questions = seed.get("questions")
    if not isinstance(questions, list) or not questions:
        raise SeedError("seed has no questions")
    qids = [q.get("id") for q in questions]
    if len(set(qids)) != len(qids) or not all(qids):
        raise SeedError("question ids must be unique and non-empty")
    if set(qids) & known:
        raise SeedError("question ids must not reuse competency ids")
    story_set = set(sids)
    primary = set()
    for q in questions:
        if not str(q.get("text", "")).strip():
            raise SeedError(f"{q['id']}: needs text")
        qc = q.get("competencies") or []
        if not qc or any(c not in known for c in qc):
            raise SeedError(f"{q['id']}: competencies must be known and non-empty")
        primary.add(qc[0])
        if any(s not in story_set for s in q.get("stories", [])):
            raise SeedError(f"{q['id']}: unknown story")
        if not q.get("stories") and not q.get("outline"):
            raise SeedError(f"{q['id']}: needs a linked story or an outline (the model answer)")
    missing = [c for c in ids if c not in primary]
    if missing:
        raise SeedError("every competency needs a question whose first competency it is: " + ", ".join(missing))


# ------------------------------------------------------------------ spaced repetition
def schedule_card(card: dict, rating: int, now: datetime) -> dict:
    """SM-2 lite. Again -> 10 minutes and reset; Hard x1.2; Good 1 d, 3 d, then x ease; Easy 4 d, then x ease x1.3."""
    if rating not in RATING_NAMES:
        raise ValueError("rating must be 1-4 (Again/Hard/Good/Easy)")
    reps = int(card.get("reps") or 0)
    ease = float(card.get("ease") or START_EASE)
    ivl = float(card.get("interval") or 0.0)
    if rating == 1:
        reps, ivl = 0, 0.0
        ease = max(MIN_EASE, ease - 0.2)
        due = now + timedelta(minutes=AGAIN_MINUTES)
    else:
        if rating == 2:
            ivl = 1.0 if reps == 0 else max(1.0, round(ivl * 1.2, 2))
            ease = max(MIN_EASE, ease - 0.15)
        elif rating == 3:
            ivl = 1.0 if reps == 0 else (3.0 if reps == 1 else round(max(ivl, 1.0) * ease, 2))
        else:
            ivl = 4.0 if reps == 0 else round(max(ivl, 1.0) * ease * 1.3, 2)
            ease = ease + 0.15
        reps += 1
        due = now + timedelta(days=ivl)
    return {"reps": reps, "ease": round(ease, 3), "interval": ivl, "due_at": _iso(due), "last_rating": rating}


def interval_label(card: dict, now: datetime) -> str:
    due = _parse(card.get("due_at"))
    if due is None:
        return ""
    mins = max(0.0, (due - now).total_seconds() / 60.0)
    if mins < 60:
        return f"{int(round(mins))} min"
    days = mins / 1440.0
    if days < 1.5:
        return "1 day"
    if days < 30:
        return f"{int(round(days))} days"
    return f"{int(round(days / 30))} mo"


def parse_outcome(outcome):
    """-> ('rate', 1..4) | ('set', 0..4) | ('skip', None) | ('known', None)."""
    if isinstance(outcome, bool):
        return ("rate", 3 if outcome else 1)
    if isinstance(outcome, int):
        if outcome in RATING_NAMES:
            return ("rate", outcome)
        raise ValueError("rating must be 1-4 (Again/Hard/Good/Easy); use 'set:N' to set mastery")
    if isinstance(outcome, dict):
        if "set" in outcome:
            return _set_value(outcome["set"])
        if "rating" in outcome:
            return parse_outcome(outcome["rating"])
        raise ValueError(f"unknown outcome: {outcome!r}")
    label = str(outcome).strip().lower()
    if label.isdigit():
        return parse_outcome(int(label))
    if label.startswith("set:"):
        return _set_value(label[4:])
    if label in RATING_WORDS:
        return ("rate", RATING_WORDS[label])
    if label in NEUTRAL:
        return ("skip", None)
    if label in KNOWN:
        return ("known", None)
    raise ValueError(f"unknown outcome: {outcome!r}")


def _set_value(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"mastery must be 0-{MAX_MASTERY}")
    if not 0 <= n <= MAX_MASTERY:
        raise ValueError(f"mastery must be 0-{MAX_MASTERY}")
    return ("set", n)


# ------------------------------------------------------------------ engine
class Learn:
    def __init__(self, db_path=None, seed_path=None, seed=None, now=None, company_dir=None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        if seed is not None:
            validate_seed(seed)
            self.seed = seed
        else:
            self.seed = load_seed(seed_path)
        self._now = now or _utc_now
        self.company_dir = Path(company_dir) if company_dir else default_company_dir()
        self.competencies = list(self.seed["competencies"])
        self.by_id = {c["id"]: c for c in self.competencies}
        self.order = {c["id"]: i for i, c in enumerate(self.competencies)}
        self.stories_list = list(self.seed.get("stories") or [])
        self.story_by_id = {s["id"]: s for s in self.stories_list}
        self.questions_list = list(self.seed["questions"])
        self.q_by_id = {q["id"]: q for q in self.questions_list}
        self.q_order = {q["id"]: i for i, q in enumerate(self.questions_list)}
        self.roles = list(self.seed.get("roles") or [])
        self.primary_qs = {c["id"]: [] for c in self.competencies}
        for q in self.questions_list:
            self.primary_qs[q["competencies"][0]].append(q)

    # compatibility alias for callers of the old Learn surface
    @property
    def concepts(self):
        return self.competencies

    def now(self) -> datetime:
        return self._now()

    # ---- storage
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.execute("CREATE TABLE IF NOT EXISTS interview_mastery(competency_id TEXT PRIMARY KEY,"
                    " level INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS interview_cards(question_id TEXT PRIMARY KEY, reps INTEGER NOT NULL,"
                    " ease REAL NOT NULL, interval_days REAL NOT NULL, due_at TEXT, last_rating INTEGER,"
                    " updated_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS interview_log(seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT,"
                    " question_id TEXT, competency_id TEXT, outcome TEXT, before INTEGER, after INTEGER)")
        return con

    def _state(self) -> dict:
        with closing(self._connect()) as con:
            rows = con.execute("SELECT competency_id, level, attempts, updated_at FROM interview_mastery").fetchall()
        st = {c["id"]: {"mastery": 0, "attempts": 0, "updated_at": None} for c in self.competencies}
        for cid, level, attempts, updated in rows:
            if cid in st:
                st[cid] = {"mastery": int(level), "attempts": int(attempts), "updated_at": updated}
        return st

    def _cards(self) -> dict:
        with closing(self._connect()) as con:
            rows = con.execute("SELECT question_id, reps, ease, interval_days, due_at, last_rating"
                               " FROM interview_cards").fetchall()
        return {qid: {"reps": int(r), "ease": float(e), "interval": float(i), "due_at": d, "last_rating": lr}
                for qid, r, e, i, d, lr in rows if qid in self.q_by_id}

    def mastery(self) -> dict:
        return {cid: s["mastery"] for cid, s in self._state().items()}

    # ---- derived state
    def _unlocked(self, cid, st) -> bool:
        return all(st[p]["mastery"] >= UNLOCK_AT for p in self.by_id[cid].get("prereqs", []))

    def status_of(self, cid, st=None) -> str:
        st = st or self._state()
        s = st[cid]
        if s["mastery"] >= UNLOCK_AT:
            return "mastered"
        if s["mastery"] > 0 or s["attempts"] > 0:
            return "learning"
        return "ready" if self._unlocked(cid, st) else "untouched"

    def _is_due(self, card, now) -> bool:
        due = _parse(card.get("due_at")) if card else None
        return due is not None and due <= now

    def _due_count(self, cards, now) -> int:
        return sum(1 for c in cards.values() if self._is_due(c, now))

    def _waves(self) -> dict:
        memo = {}

        def depth(cid):
            if cid not in memo:
                pre = self.by_id[cid].get("prereqs", [])
                memo[cid] = 0 if not pre else 1 + max(depth(p) for p in pre)
            return memo[cid]

        return {c["id"]: depth(c["id"]) for c in self.competencies}

    def _reduced_edges(self):
        """Prerequisite edges with implied (transitive) ones removed, for drawing."""
        anc = {}

        def ancestors(cid):
            if cid not in anc:
                out = set()
                for p in self.by_id[cid].get("prereqs", []):
                    out.add(p)
                    out |= ancestors(p)
                anc[cid] = out
            return anc[cid]

        edges = []
        for c in self.competencies:
            pre = c.get("prereqs", [])
            for p in pre:
                if not any(p in ancestors(q) for q in pre if q != p):
                    edges.append([p, c["id"]])
        return edges

    # ---- payload builders
    def _story_ref(self, sid):
        s = self.story_by_id.get(sid)
        return {"id": sid, "title": s["title"]} if s else None

    def model_answer(self, q) -> dict:
        """The bullet outline revealed by 'Show model answer'."""
        story_ids = [s for s in q.get("stories", []) if s in self.story_by_id]
        best = self.story_by_id[story_ids[0]] if story_ids else None
        bullets = []
        if q.get("outline"):
            bullets = [{"label": "", "text": t} for t in q["outline"]]
        elif best:
            for label, key in (("Situation", "situation"), ("Turning point", "pivot"), ("Action", "action"),
                               ("Result", "result")):
                if str(best.get(key, "")).strip():
                    bullets.append({"label": label, "text": best[key]})
            link = q.get("angle") or best.get("link")
            if link:
                bullets.append({"label": "Link", "text": link})
        out = {
            "kind": "outline" if q.get("outline") else "story",
            "bullets": bullets,
            "angle": q.get("angle"),
            "story": {"id": best["id"], "title": best["title"]} if best else None,
            "sixty": best.get("sixty") if best else None,
            "say_it": best.get("say_it") if best else None,
            "limits": best.get("limits") if best else None,
            "evidence": list(best.get("evidence", [])) if best else [],
            "also": [self._story_ref(s) for s in story_ids[1:]],
        }
        return out

    def _question_view(self, q) -> dict:
        return {"id": q["id"], "text": q["text"], "competencies": list(q["competencies"]),
                "competency_titles": [self.by_id[c]["title"] for c in q["competencies"]],
                "stories": list(q.get("stories", [])), "roles": list(q.get("roles", [])),
                "weight": q.get("weight", 1), "model_answer": self.model_answer(q)}

    def _step(self, q, st, cards, now, mode="adaptive") -> dict:
        cid = q["competencies"][0]
        c = self.by_id[cid]
        s = st[cid]
        card = cards.get(q["id"]) or {"reps": 0, "ease": START_EASE, "interval": 0.0, "due_at": None, "last_rating": None}
        preview = {}
        for r, name in RATING_NAMES.items():
            preview[name] = interval_label(schedule_card(card, r, now), now)
        ma = self.model_answer(q)
        return {
            "mode": mode,
            # competency
            "competency": cid, "concept": cid, "title": c["title"], "group": c.get("group"),
            "mastery": s["mastery"], "status": self.status_of(cid, st), "explain": c.get("explain", ""),
            "builds_on": [{"id": p, "title": self.by_id[p]["title"]} for p in c.get("prereqs", [])],
            "unlocks": [x["id"] for x in self.competencies if cid in x.get("prereqs", [])],
            # question
            "question": {"id": q["id"], "text": q["text"],
                         "also_tests": [{"id": x, "title": self.by_id[x]["title"]} for x in q["competencies"][1:]]},
            "check_question": q["text"], "options": None, "answer": None,
            "model_answer": ma,
            "card": {"reps": card["reps"], "interval": card["interval"], "due_at": card["due_at"],
                     "due": self._is_due(card, now), "new": q["id"] not in cards},
            "intervals": preview,
            "evidence": ma["evidence"] or list(c.get("evidence", [])),
            "remaining": sum(1 for x in self.competencies if st[x["id"]]["mastery"] < UNLOCK_AT),
            "due_total": self._due_count(cards, now),
        }

    def _pick_question(self, cid, cards, now):
        qs = self.primary_qs[cid]
        due = sorted((q for q in qs if self._is_due(cards.get(q["id"]), now)),
                     key=lambda q: (_parse(cards[q["id"]]["due_at"]), self.q_order[q["id"]]))
        if due:
            return due[0]
        unseen = [q for q in qs if q["id"] not in cards]
        if unseen:
            return unseen[0]
        return min(qs, key=lambda q: (_parse(cards[q["id"]]["due_at"]) or now, self.q_order[q["id"]]))

    def next_step(self):
        """One small step: the weakest competency whose prerequisites are all >= 3 (ties: one with a
        due card, then seed order), and one of its questions. Once every competency is at full
        mastery, only due cards are offered; returns None when nothing is left to do now."""
        st, cards, now = self._state(), self._cards(), self.now()
        has_due = {cid: any(self._is_due(cards.get(q["id"]), now) for q in qs) for cid, qs in self.primary_qs.items()}
        cands = [c for c in self.competencies
                 if self._unlocked(c["id"], st) and (st[c["id"]]["mastery"] < MAX_MASTERY or has_due[c["id"]])]
        if not cands:
            return None
        c = min(cands, key=lambda x: (st[x["id"]]["mastery"], 0 if has_due[x["id"]] else 1, self.order[x["id"]]))
        return self._step(self._pick_question(c["id"], cards, now), st, cards, now)

    def question_step(self, question_id, mode="chosen") -> dict:
        if question_id not in self.q_by_id:
            raise KeyError(f"unknown question: {question_id}")
        return self._step(self.q_by_id[question_id], self._state(), self._cards(), self.now(), mode)

    # ---- recording
    def record(self, concept_id, outcome) -> dict:
        """Rate a question (``concept_id`` = question id; outcome 1-4 or again/hard/good/easy) or adjust
        a competency directly (``concept_id`` = competency id; rating, 'set:N', 'known' or 'skip')."""
        if concept_id in self.q_by_id:
            qid, cid = concept_id, self.q_by_id[concept_id]["competencies"][0]
        elif concept_id in self.by_id:
            qid, cid = None, concept_id
        else:
            raise KeyError(f"unknown question or competency: {concept_id}")
        kind, value = parse_outcome(outcome)
        now = self.now()
        st = self._state()[cid]
        before, attempts = st["mastery"], st["attempts"]
        card = None
        if kind == "rate":
            after = max(0, min(MAX_MASTERY, before + MASTERY_DELTA[value]))
            attempts += 1
            label = RATING_NAMES[value]
            if qid:
                card = schedule_card(self._cards().get(qid) or {}, value, now)
        elif kind == "set":
            after, label = value, f"set:{value}"
        elif kind == "known":
            after, label = MAX_MASTERY, "known"
            if qid:
                card = schedule_card(self._cards().get(qid) or {}, 4, now)
        else:
            after, label = before, "skip"
        stamp = _iso(now)
        with closing(self._connect()) as con:
            with con:
                con.execute("INSERT INTO interview_mastery(competency_id, level, attempts, updated_at) VALUES(?,?,?,?)"
                            " ON CONFLICT(competency_id) DO UPDATE SET level=excluded.level,"
                            " attempts=excluded.attempts, updated_at=excluded.updated_at",
                            (cid, after, attempts, stamp))
                if card is not None:
                    con.execute("INSERT INTO interview_cards(question_id, reps, ease, interval_days, due_at,"
                                " last_rating, updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE"
                                " SET reps=excluded.reps, ease=excluded.ease, interval_days=excluded.interval_days,"
                                " due_at=excluded.due_at, last_rating=excluded.last_rating,"
                                " updated_at=excluded.updated_at",
                                (qid, card["reps"], card["ease"], card["interval"], card["due_at"],
                                 card["last_rating"], stamp))
                con.execute("INSERT INTO interview_log(at, question_id, competency_id, outcome, before, after)"
                            " VALUES(?,?,?,?,?,?)", (stamp, qid, cid, label, before, after))
        out = {"question": qid, "competency": cid, "concept": cid, "outcome": label, "before": before,
               "mastery": after, "status": self.status_of(cid), "next": self.next_step()}
        if card is not None:
            out["card"] = card
            out["next_review"] = interval_label(card, now)
        return out

    # ---- listings
    def stories(self) -> list:
        out = []
        for s in self.stories_list:
            d = {k: s.get(k) for k in ("id", "title", "situation", "task", "action", "result", "pivot", "link",
                                       "sixty", "say_it", "limits", "evidence", "competencies", "roles")}
            d["concepts"] = list(s.get("competencies", []))
            d["competency_titles"] = [self.by_id[c]["title"] for c in s.get("competencies", []) if c in self.by_id]
            d["questions"] = [q["id"] for q in self.questions_list if s["id"] in q.get("stories", [])]
            out.append(d)
        return out

    def questions(self, full=False) -> list:
        """Every question; ``full`` adds the model answer (the snapshot stays small without it)."""
        if full:
            return [self._question_view(q) for q in self.questions_list]
        return [{k: v for k, v in self._question_view(q).items() if k != "model_answer"} for q in self.questions_list]

    def _interview_order(self, q):
        group = self.by_id[q["competencies"][0]].get("group")
        return (GROUP_ORDER.get(group, 9), self.q_order[q["id"]])

    def concept_graph(self) -> dict:
        st, cards, now = self._state(), self._cards(), self.now()
        waves = self._waves()
        nodes = []
        counts = {k: 0 for k in LEARN_TO_BOARD}
        for c in self.competencies:
            ls = self.status_of(c["id"], st)
            counts[ls] += 1
            qs = [q["id"] for q in self.questions_list if c["id"] in q["competencies"]]
            nodes.append({
                "id": c["id"], "title": c["title"], "status": LEARN_TO_BOARD[ls],
                "display_status": LEARN_TO_BOARD[ls], "learn_status": ls, "group": c.get("group"),
                "mastery": st[c["id"]]["mastery"], "attempts": st[c["id"]]["attempts"],
                "deps": list(c.get("prereqs", [])), "wave": waves[c["id"]],
                "lane": c.get("group") or "interview", "cluster": c.get("group") or "interview",
                "weight": 1 + st[c["id"]]["mastery"],
                "explain": c.get("explain", ""), "evidence": list(c.get("evidence", [])),
                "questions": qs, "due": sum(1 for q in qs if self._is_due(cards.get(q), now)),
                "stories": [s["id"] for s in self.stories_list if c["id"] in s.get("competencies", [])],
            })
        edges = self._reduced_edges()
        return {"project": "interview", "title": self.seed.get("title", "Interview"), "nodes": nodes,
                "edges": edges, "links": [{"source": a, "target": b, "kind": "prereq"} for a, b in edges],
                "counts": counts, "max_mastery": MAX_MASTERY, "unlock_at": UNLOCK_AT,
                "due": self._due_count(cards, now)}

    # ---- before an interview
    def _match_roles(self, text) -> list:
        low = " " + " ".join(_words(text)) + " "
        hits = []
        for r in self.roles:
            score = 0
            for m in r.get("match", []):
                phrase = " ".join(_words(m))
                if phrase and f" {phrase} " in low:
                    score += 2 if " " in phrase else 1
            if score:
                hits.append((score, r))
        hits.sort(key=lambda x: -x[0])
        return [r for _, r in hits]

    def _company_files(self, text) -> list:
        low = " " + " ".join(_words(text)) + " "
        try:
            names = sorted(os.listdir(self.company_dir))
        except OSError:
            return []
        out = []
        for name in names:
            if not name.lower().endswith(".md") or " - " not in name:
                continue
            company = " ".join(_words(name.split(" - ", 1)[0].replace("&", " ")))
            first = company.split(" ")[0] if company else ""
            if company and (f" {company} " in low or (len(first) >= 4 and f" {first} " in low)):
                out.append(str(self.company_dir / name))
        return out

    def prep(self, text) -> dict:
        """Before an interview: the 10 most relevant questions and the stories that answer them."""
        text = str(text or "").strip()
        roles = self._match_roles(text)
        role_ids = [r["id"] for r in roles]
        words = {w for w in _words(text) if w not in STOP and len(w) > 1}
        st = self._state()
        n_roles = max(1, len(self.roles))
        # Core questions (weight >= 3: tell me about yourself, why us, questions for them) always come.
        picked = [q for q in self.questions_list if q.get("weight", 1) >= 3][:PREP_QUESTIONS]
        per = {}
        for q in picked:
            per[q["competencies"][0]] = per.get(q["competencies"][0], 0) + 1
        scored = []
        for q in self.questions_list:
            if q in picked:
                continue
            score = float(q.get("weight", 1))
            qroles = set(q.get("roles", [])) or {r["id"] for r in self.roles}
            specific = len(qroles) < n_roles
            if role_ids:
                # A question written for exactly this role family beats a generic one.
                if qroles & set(role_ids[:1]):
                    score += 4.0 / len(qroles)
                elif qroles & set(role_ids):
                    score += 2.0 / len(qroles)
                elif specific:
                    score -= 3
            elif specific:
                score -= 1.5
            qwords = set(_words(" ".join([q["text"]] + list(q.get("tags", [])))))
            score += 1.0 * len(words & qwords)
            if st[q["competencies"][0]]["mastery"] < UNLOCK_AT:
                score += 0.5
            scored.append((-score, self.q_order[q["id"]], q))
        scored.sort(key=lambda x: (x[0], x[1]))
        groups = {}
        for q in picked:
            g = self.by_id[q["competencies"][0]].get("group")
            groups[g] = groups.get(g, 0) + 1

        def take(q):
            cid = q["competencies"][0]
            g = self.by_id[cid].get("group")
            cap = 1 if g == "behavioural" else PREP_PER_COMPETENCY
            if q in picked or per.get(cid, 0) >= cap or len(picked) >= PREP_QUESTIONS:
                return False
            if groups.get(g, 0) >= PREP_GROUP_CAP.get(g, PREP_QUESTIONS):
                return False
            picked.append(q)
            per[cid] = per.get(cid, 0) + 1
            groups[g] = groups.get(g, 0) + 1
            return True

        # Every interview has behavioural questions: reserve slots for the best STAR ones first.
        reserved = 0
        for _, _, q in scored:
            if reserved >= PREP_BEHAVIOURAL:
                break
            if self.by_id[q["competencies"][0]].get("group") == "behavioural" and take(q):
                reserved += 1
        for _, _, q in scored:
            take(q)
        story_score = {}
        for rank, q in enumerate(picked):
            for i, sid in enumerate(q.get("stories", [])):
                story_score[sid] = story_score.get(sid, 0.0) + (2.0 if i == 0 else 1.0) + (PREP_QUESTIONS - rank) * 0.05
        for s in self.stories_list:
            if s["id"] in story_score and set(s.get("roles", [])) & set(role_ids):
                story_score[s["id"]] += 1.0
        top = sorted(story_score, key=lambda sid: (-story_score[sid], self.stories_list.index(self.story_by_id[sid])))
        stories_by_id = {s["id"]: s for s in self.stories()}
        return {
            "query": text,
            "roles": [{"id": r["id"], "label": r["label"]} for r in roles],
            "company_files": self._company_files(text),
            "role_files": list(roles[0].get("evidence", [])) if roles else [],
            "questions": [self._question_view(q) for q in sorted(picked, key=self._interview_order)],
            "stories": [stories_by_id[sid] for sid in top[:PREP_STORIES]],
            "note": ("Matched " + ", ".join(r["label"] for r in roles) if roles
                     else "No role family matched; showing the questions most interviews ask."),
        }

    # ---- live practice in the Gemini app (Owner copies the prompt and talks in Gemini Live)
    def live_prompt(self, company="", role="") -> dict:
        """A spoken mock-interviewer prompt for Gemini Live, built from ``prep()``: the role, the same
        10 questions in interview order, the matching STAR stories and each story's evidence limits."""
        company, role = str(company or "").strip(), str(role or "").strip()
        if not company and not role:
            raise ValueError("give a company or a role, e.g. ?company=Acme&role=RQA analyst")
        p = self.prep(" ".join(x for x in (company, role) if x))
        target = (role + " at " + company) if company and role else (role or company)
        stories = p["stories"]
        num = {s["id"]: i + 1 for i, s in enumerate(stories)}
        lines = [
            "You are a professional interviewer running a spoken mock interview with the owner, "
            "a graduate candidate. We are talking by voice, so keep every turn short and natural.",
            "",
            f"The role: {target}.",
        ]
        if p["roles"]:
            lines.append("Role family: " + ", ".join(r["label"] for r in p["roles"]) + ".")
        lines += [
            "",
            "How to run the interview",
            f"- Introduce yourself in one sentence as an interviewer{(' at ' + company) if company else ''}, then ask question 1.",
            "- Ask one question at a time, then stop and listen until Owner has finished. Do not answer for him "
            "and do not list options.",
            "- After each answer, ask one or two short follow-up questions that probe: his own part as opposed to "
            "the team's, how a number was measured, what he would do differently. Then move on.",
            "- Stay in role. Do not coach or give feedback during the interview.",
            "- Do not invent facts about Owner. The stories below are the evidence; if he claims more than a "
            "story's limits allow, probe it politely, as a real interviewer would.",
            "- At the end (after the last question, or when Owner says \"end interview\"), give brief feedback: "
            "three things that worked, three to improve, and one line on any claim that went beyond the evidence. "
            "Keep it under two minutes of speech.",
            "",
            "Questions, in order",
        ]
        for i, q in enumerate(p["questions"], 1):
            fit = [num[s] for s in q.get("stories", []) if s in num]
            hint = f" (best stories: {', '.join('S' + str(n) for n in fit)})" if fit else ""
            lines.append(f"{i}. {q['text']}{hint}")
        if stories:
            lines += ["", "Owner's stories (facts from his evidence files; do not add to them)"]
            for i, s in enumerate(stories, 1):
                lines.append(f"S{i}. {s['title']}")
                for label, key in (("Situation", "situation"), ("Task", "task"), ("Action", "action"),
                                   ("Result", "result")):
                    if str(s.get(key) or "").strip():
                        lines.append(f"   {label}: {s[key]}")
                lines.append("   Evidence cannot support: " + (str(s.get("limits") or "").strip()
                                                              or "anything beyond the lines above."))
        lines += ["", "Start now with your one-sentence introduction and question 1."]
        return {"company": company, "role": role, "target": target, "prompt": "\n".join(lines),
                "questions": [q["id"] for q in p["questions"]], "stories": [s["id"] for s in stories],
                "note": p["note"]}

    def snapshot(self) -> dict:
        """Payload for GET /api/interview (and a superset of GET /api/learn)."""
        graph = self.concept_graph()
        return {"schema_version": self.seed.get("schema_version"), "title": self.seed.get("title", "Interview"),
                "next": self.next_step(), "graph": graph, "stories": self.stories(), "questions": self.questions(),
                "roles": [{"id": r["id"], "label": r["label"]} for r in self.roles],
                "counts": graph["counts"], "total": len(self.competencies), "due": graph["due"]}


Interview = Learn

# ---- module-level API (CONTRACT: concept_graph(), next_step(), record(), stories())
_default = None


def _get(db_path=None, seed_path=None) -> Learn:
    global _default
    if db_path or seed_path:
        return Learn(db_path, seed_path)
    if _default is None:
        _default = Learn()
    return _default


def concept_graph(db_path=None, seed_path=None) -> dict:
    return _get(db_path, seed_path).concept_graph()


def next_step(db_path=None, seed_path=None):
    return _get(db_path, seed_path).next_step()


def record(concept_id, outcome, db_path=None, seed_path=None) -> dict:
    return _get(db_path, seed_path).record(concept_id, outcome)


def stories(db_path=None, seed_path=None) -> list:
    return _get(db_path, seed_path).stories()


def questions(db_path=None, seed_path=None) -> list:
    return _get(db_path, seed_path).questions()


def question_step(question_id, db_path=None, seed_path=None) -> dict:
    return _get(db_path, seed_path).question_step(question_id)


def prep(text, db_path=None, seed_path=None) -> dict:
    return _get(db_path, seed_path).prep(text)


def snapshot(db_path=None, seed_path=None) -> dict:
    return _get(db_path, seed_path).snapshot()


def live_prompt(company="", role="", db_path=None, seed_path=None) -> dict:
    return _get(db_path, seed_path).live_prompt(company, role)


# ---- extension routes (CONTRACT "Extension routes"; api.py loads ROUTES for "learn")
def _r_interview(h, q):
    return snapshot()


def _r_prep(h, q):
    text = q.get("q") or q.get("text") or " ".join(x for x in (q.get("company"), q.get("role")) if x)
    if not str(text).strip():
        raise ValueError("give a company or role, e.g. ?q=Acme risk analyst")
    return prep(text)


def _r_question(h, q):
    if not q.get("id"):
        raise ValueError("missing id")
    return question_step(q["id"])


def _p_rate(h, q):
    b = h._json_body() or {}
    target = b.get("question") or b.get("concept")
    rating = b.get("rating", b.get("outcome"))
    if not target or rating in (None, ""):
        raise ValueError("missing question or rating")
    return {"ok": True, "result": record(target, rating)}


def _r_live_prompt(h, q):
    company, role = q.get("company") or "", q.get("role") or ""
    if not (company.strip() or role.strip()) and (q.get("q") or "").strip():
        role = q["q"]
    return live_prompt(company, role)


ROUTES = {
    "GET /api/interview": _r_interview,
    "GET /api/interview/prep": _r_prep,
    "GET /api/learn/live_prompt": _r_live_prompt,
    "GET /api/interview/live_prompt": _r_live_prompt,
    "GET /api/interview/question": _r_question,
    "POST /api/interview/rate": _p_rate,
}
