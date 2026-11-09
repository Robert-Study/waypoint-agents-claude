"""Agent board tools (MCP, newline-delimited JSON-RPC on stdio, stdlib only) over the Waypoint data folder.

Tools: board_read, toolbox_read, dashboard_read, todo_add, todo_tick, node_status, node_move, node_add, node_edit, claim, release,
ask, answer.
ask / answer: a needs_you question carries the problem, a proposed resolution, options and the recommended one
(questions/<id>.json, the same file resume.py routes); Owner answers in one click and the answer goes straight back to
the owning session (warm: delivered; cold: the node returns to ready for a fresh worker).
node_add / node_edit plan in place (waypoint_data.nodes: the goal/check locks of engine plan.py).
Every folder access goes through core/waypoint-data/py (data-pyclient): Store, Claims, compute_float. No other file
access. Agents use these tools, never hand edits.

Identity: ``by`` is fixed when the server starts (env WAYPOINT_MCP_BY, default "agent"), never taken from a tool call,
so an agent cannot pose as the owner. Only a server started with by="owner" may set a node done/skipped/carried or
write without holding the node's claim. No usage-limit constant lives here.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

try:
    import waypoint_data  # noqa: F401
except ImportError:  # running from a checkout: core/waypoint-data/py sits beside runner/
    for _py in (Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py",  # source checkout
                Path(__file__).resolve().parents[2] / "waypoint-data" / "py"):  # the Waypoint Core product tree
        if _py.is_dir():
            sys.path.insert(0, str(_py))
            break
from waypoint_data import Claims, RevConflict, Store, compute_float  # noqa: E402
from waypoint_data import nodes as wn  # noqa: E402
from waypoint_data.store import iso, parse_iso  # noqa: E402

try:
    from . import resume  # noqa: E402
except ImportError:  # started as a script from this folder
    import resume  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATUSES = ("not_started", "ready", "running", "submitted", "done", "error", "needs_you", "skipped", "carried")
OWNER_ONLY_STATUSES = ("done", "skipped", "carried")  # an agent hands work in as submitted; the owner or check closes it
FINISHED = ("done", "skipped", "carried")
CATEGORY_SUBTYPE = {"review": "failure", "decisions": "approval", "hands_on": "own"}
DECISIONS = ("approve", "option", "reject", "reply", "answers")
ITEM_KINDS = ("choice", "multi", "scale", "text")  # scale = 1-9, how strongly Owner agrees
NO_QUESTION = "(no question was written for this needs_you task)"


class ToolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


def _id(value, what):
    if not isinstance(value, str) or not _ID.match(value):
        raise ToolError("bad_argument", f"{what} must match {_ID.pattern}")
    return value


def _int(value, what, minimum=0, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ToolError("bad_argument", f"{what} must be an integer >= {minimum}")
    return value


class BoardTools:
    def __init__(self, root, by="agent", now=time.time):
        self.root = os.path.abspath(root)
        self.by = by
        self.now = now
        self.store = Store(self.root, writer=by, now=now)
        self.claims = Claims(self.root, now=now, writer=by)
        self.desktop = resume.desktop_activity  # how answer() tells a warm asking session from a cold one

    # ---------------------------------------------------------------- helpers
    def _project_dirs(self):
        """Folder names under projects/. The data client lists files only (folders are not entities), so the
        names come from a plain directory listing at the Store's own path; every entity is still read via Store."""
        try:
            return sorted(n for n in os.listdir(self.store.path("projects"))
                          if os.path.isdir(self.store.path(f"projects/{n}")))
        except OSError:
            return []

    def _nodes(self, project):
        _id(project, "project")
        if not self.store.exists(f"projects/{project}/project.json"):
            raise ToolError("not_found", f"project {project} does not exist")
        return self.store.list_entities(f"projects/{project}/nodes")

    def _node(self, project, node):
        _id(project, "project")
        _id(node, "node")
        n = self.store.read(f"projects/{project}/nodes/{node}.json")
        if n is None:
            raise ToolError("not_found", f"node {project}/{node} does not exist")
        return n

    def _live_claim(self, project, node):
        c = self.claims.read(project, node)
        if isinstance(c, dict) and not self.claims.is_stale(c):
            return c
        return None

    @staticmethod
    def _conflict(exc, what):
        cur = exc.current if isinstance(exc.current, dict) else {}
        return ToolError("rev_conflict", f"{what} changed since you read it; re-read and retry",
                         {"current_rev": cur.get("rev"), "current": exc.current})

    # ---------------------------------------------------------------- tools
    def board_read(self, args):
        project = args.get("project")
        if project is None:
            out = []
            for n in self._project_dirs():
                if not _ID.match(n):
                    continue
                p = self.store.read(f"projects/{n}/project.json")
                if isinstance(p, dict) and not p.get("merged_into"):  # a merged-away version is history only
                    out.append({"id": p["id"], "title": p.get("title"), "status": p.get("status"),
                                "archived": p.get("archived"), "rev": p.get("rev"), "version": p.get("version"),
                                "version_label": p.get("version_label"), "version_range": p.get("version_range"),
                                "family": p.get("family"),
                                "subtitle": p.get("subtitle")})
            out.sort(key=lambda p: (p.get("family") or p["id"], -(p.get("version") or 0)))  # newest version first
            return {"projects": out, "todos": self._open_todos(None), "needs": self._open_needs(None)}
        nodes = self._nodes(project)
        try:
            fl = {f["id"]: f for f in compute_float(nodes)}
        except ValueError as exc:  # a dependency cycle: still show the board, without float
            fl = {}
            cycle = str(exc)
        else:
            cycle = None
        needs = self._open_needs(project)
        rows = []
        for n in sorted(nodes, key=lambda x: x["id"]):
            f = fl.get(n["id"], {"early": None, "late": None, "float": None, "critical": None})
            c = self._live_claim(project, n["id"])
            rows.append({"id": n["id"], "title": n.get("title"), "status": n["status"], "rev": n["rev"],
                         "slot": n.get("slot"), "role": n.get("role"), "kind": n.get("kind"),
                         "lane": n.get("lane"), "estimate": n.get("estimate"),
                         "tags": n.get("tags") or [],
                         "deps": [d if isinstance(d, str) else d["id"] for d in n.get("deps") or []],
                         "float": {"early": f["early"], "late": f["late"], "float": f["float"], "critical": f["critical"]},
                         "claim": ({"owner": c["owner"], "claimed_by": c.get("claimed_by"), "since": c.get("since")}
                                   if c else None),
                         "needs": [q for q in needs if q["node"] == n["id"]]})
        return {"project": self.store.read(f"projects/{project}/project.json"), "nodes": rows, "cycle": cycle,
                "todos": self._open_todos(project), "needs": needs}

    def _questions(self):
        try:
            return [q for q in self.store.list_entities("questions") if isinstance(q, dict) and q.get("id")]
        except Exception:  # noqa: BLE001 - no questions folder yet
            return []

    def _open_needs(self, project):
        """Unanswered questions (one project, or all): what Owner sees in a needs-you dropdown."""
        out = [{"id": q["id"], "project": q.get("project"), "node": q.get("node"), "problem": q.get("prompt"),
                "resolution": q.get("resolution"), "options": q.get("options"), "recommended": q.get("recommended"),
                "items": q.get("items"), "kind": q.get("kind"), "created_at": q.get("created_at"),
                "explain": q.get("explain"), "option_notes": q.get("option_notes"), "session": q.get("session"),
                "snooze_until": q.get("snooze_until")}
               for q in self._questions() if not q.get("answered_at") and q.get("answer") is None
               and (project is None or q.get("project") == project)]
        out.sort(key=lambda x: (str(x["created_at"] or ""), x["id"]))
        return out

    def _open_todos(self, project):
        """Open to-dos for one project, or for every project and none (project None: Owner's one human list)."""
        todos = [t for t in self.store.list_entities("todos") if t.get("status") == "open"
                 and (project is None or t.get("project") == project)]
        todos.sort(key=lambda t: (t.get("order") or 0, t["id"]))
        return [{"id": t["id"], "text": t["text"], "category": t["category"], "rev": t["rev"],
                 "project": t.get("project"), "order": t.get("order"), "link": t.get("link"),
                 "session": t.get("session"), "snooze_until": t.get("snooze_until")} for t in todos]

    def toolbox_read(self, _args):
        """Owner's toolbox (tools, MCPs, CLIs, models, routes, subscriptions) from toolbox.json in the folder."""
        t = self.store.read("toolbox.json")
        if not isinstance(t, dict):
            return {"items": [], "missing": True}
        return t

    def dashboard_read(self, _args):
        """Runs, open questions, usage pools (remaining percent) and agent toggles for the dashboard page.
        Limits stay in Waypoint settings; this server only reports what the usage cache observed."""
        cache = self.store.read("usage_cache.json")
        pools = []
        for src in ((cache or {}).get("sources") or {}).values():
            for r in (src or {}).get("rows") or []:
                if isinstance(r, dict) and r.get("pool"):
                    pools.append({"pool": r["pool"], "remaining_percent": r.get("remaining_percent"),
                                  "resets_at": r.get("resets_at"), "observed_at": r.get("observed_at")})
        return {"runs": self.store.list_entities("runs"),
                "questions": [q for q in self.store.list_entities("questions") if not q.get("answered_at")],
                "usage": {"pools": pools},
                "toggles": [t for t in self.store.list_entities("agents") if isinstance(t, dict) and "enabled" in t]}

    def todo_add(self, args):
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("bad_argument", "text is required")
        category = args.get("category", "hands_on")
        if category not in CATEGORY_SUBTYPE:
            raise ToolError("bad_argument", f"category must be one of {sorted(CATEGORY_SUBTYPE)}")
        project, node = args.get("project"), args.get("node")
        if project is not None:
            _id(project, "project")
        if node is not None:
            if project is None:
                raise ToolError("bad_argument", "node needs project")
            _id(node, "node")
        link = args.get("link")
        if link is not None and not isinstance(link, str):
            raise ToolError("bad_argument", "link must be a string")
        session = args.get("session")
        if session is not None and (not isinstance(session, str) or not session.strip() or len(session) > 200):
            raise ToolError("bad_argument", "session must be a session id")
        existing = self.store.list_entities("todos")
        order = 1 + max([t.get("order") or 0 for t in existing if t.get("category") == category] or [0])
        tid = "t-" + secrets.token_hex(4)
        entity = {"schema": "waypoint.todo.v1", "id": tid, "text": text.strip(), "status": "open", "category": category,
                  "subtype": CATEGORY_SUBTYPE[category], "link": link,
                  "node": {"project": project, "id": node} if node else None, "project": project, "order": order,
                  "merged_into": None, "click_count": 0, "next_click": None, "session": session,
                  "created_at": iso(self.now()), "created_by": self.by}
        return self.store.write(f"todos/{tid}.json", entity, expected_rev=0)

    def todo_tick(self, args):
        tid = _id(args.get("id"), "id")
        rel = f"todos/{tid}.json"
        t = self.store.read(rel)
        if t is None:
            raise ToolError("not_found", f"todo {tid} does not exist")
        exp = _int(args.get("expected_rev"), "expected_rev", 1, allow_none=True)
        if exp is not None and exp != t.get("rev"):
            raise ToolError("rev_conflict", "todo changed since you read it", {"current_rev": t.get("rev"), "current": t})
        if t.get("status") == "done":
            return t
        if t.get("status") != "open":
            raise ToolError("bad_state", f"todo is {t.get('status')}, only an open to-do can be ticked")
        new = dict(t, status="done", done_by=self.by, archived_at=iso(self.now()))
        try:
            return self.store.write(rel, new, expected_rev=t["rev"])
        except RevConflict as exc:
            raise self._conflict(exc, "todo")

    def snooze(self, args):
        """Hide one open to-do or question from Owner's list until a time (ISO), or null to bring it back."""
        kind = args.get("kind")
        if kind not in ("todo", "question"):
            raise ToolError("bad_argument", "kind must be todo or question")
        rel = f"{'todos' if kind == 'todo' else 'questions'}/{_id(args.get('id'), 'id')}.json"
        cur = self.store.read(rel)
        if not isinstance(cur, dict):
            raise ToolError("not_found", f"{kind} {args.get('id')} does not exist")
        until = args.get("until")
        if until is not None:
            try:
                ok = isinstance(until, str) and parse_iso(until) is not None
            except (TypeError, ValueError, AttributeError):
                ok = False
            if not ok:
                raise ToolError("bad_argument", "until must be an ISO time or null")
        try:
            out = self.store.write(rel, dict(cur, snooze_until=until), expected_rev=cur["rev"])
        except RevConflict as exc:
            raise self._conflict(exc, kind)
        self.store.append_event(self.by, {"kind": f"{kind}.snooze", "summary": f"Owner snoozed {kind} {cur['id']}"
                                          + (f" until {until}." if until else " off."), "project": cur.get("project"),
                                          "data": {"id": cur["id"], "until": until}})
        return {"id": out["id"], "kind": kind, "snooze_until": until}

    def node_status(self, args):
        project, nid = args.get("project"), args.get("node")
        status = args.get("status")
        if status not in STATUSES:
            raise ToolError("bad_argument", f"status must be one of {list(STATUSES)}")
        exp = _int(args.get("expected_rev"), "expected_rev", 1)  # required: a write names the rev it read
        n = self._node(project, nid)
        is_owner = self.by == "owner"
        if status in OWNER_ONLY_STATUSES and not is_owner:
            raise ToolError("owner_only", f"only the owner sets {status}; submit the work and the check or owner closes it")
        if not is_owner:
            c = self._live_claim(project, nid)
            if c is None:
                raise ToolError("not_claimed", "claim the node before changing its status")
            if args.get("token") != c.get("token"):
                raise ToolError("claimed_by_other", f"node is claimed by {c.get('claimed_by') or c.get('owner')}")
        if exp != n.get("rev"):
            raise ToolError("rev_conflict", f"node is at rev {n.get('rev')}, you sent {exp}",
                            {"current_rev": n.get("rev"), "current": n})
        new = dict(n, status=status)
        for k in ("evidence", "actor"):
            if isinstance(args.get(k), str):
                new[k] = args[k]
        try:
            return self.store.write(f"projects/{project}/nodes/{nid}.json", new, expected_rev=exp)
        except RevConflict as exc:
            raise self._conflict(exc, "node")

    def node_move(self, args):
        project, nid = args.get("project"), args.get("node")
        slot = _int(args.get("slot"), "slot", 0, allow_none=True)
        nodes = self._nodes(project)
        n = self._node(project, nid)
        exp = _int(args.get("expected_rev"), "expected_rev", 1, allow_none=True)
        if exp is not None and exp != n.get("rev"):
            raise ToolError("rev_conflict", f"node is at rev {n.get('rev')}, you sent {exp}",
                            {"current_rev": n.get("rev"), "current": n})
        if n["status"] in FINISHED:
            raise ToolError("bad_state", "a finished node cannot be moved")
        try:
            f = {x["id"]: x for x in compute_float(nodes)}[nid]
        except ValueError as exc:
            raise ToolError("bad_state", f"the plan has a dependency cycle, float is unknown ({exc})")
        if slot is not None and not (f["early"] <= slot <= f["late"]):
            raise ToolError("outside_float", f"slot {slot} is outside the float [{f['early']}, {f['late']}]",
                            {"early": f["early"], "late": f["late"], "float": f["float"]})
        try:
            return self.store.write(f"projects/{project}/nodes/{nid}.json", dict(n, slot=slot), expected_rev=n["rev"])
        except RevConflict as exc:
            raise self._conflict(exc, "node")

    def _graph_ok(self, project, entity):
        """Refuse unknown deps or a cycle once ``entity`` replaces (or joins) the project's nodes."""
        nodes = [n for n in self._nodes(project) if n["id"] != entity["id"]] + [entity]
        known = {n["id"] for n in nodes}
        unknown = [wn.dep_id(d) for d in entity.get("deps") or [] if wn.dep_id(d) not in known]
        if unknown or (entity.get("parent") and entity["parent"] not in known):
            raise ToolError("bad_argument", f"unknown dependency or parent: {unknown or [entity['parent']]}")
        try:
            compute_float(nodes)
        except ValueError as exc:
            raise ToolError("bad_state", f"that edit makes a {exc}")

    def node_add(self, args):
        project, nid = _id(args.get("project"), "project"), _id(args.get("node"), "node")
        fields = {k: args[k] for k in wn.EDITABLE + ("status", "evidence") if k in args}
        try:
            entity = wn.build_node(project, nid, fields, is_owner=self.by == "owner")
        except wn.GoalLocked as exc:
            raise ToolError("owner_only", str(exc))
        except wn.NodeError as exc:
            raise ToolError("bad_argument", str(exc))
        if self.store.exists(f"projects/{project}/nodes/{nid}.json"):
            raise ToolError("exists", f"node {project}/{nid} already exists; use node_edit")
        self._graph_ok(project, entity)
        try:
            out = self.store.write(f"projects/{project}/nodes/{nid}.json", dict(entity, actor=self.by), expected_rev=0)
        except RevConflict as exc:
            raise ToolError("exists", f"node {project}/{nid} already exists; use node_edit", {"current": exc.current})
        self.store.append_event(self.by, {"kind": "node.add", "summary": f"{self.by} added task {nid}.",
                                          "project": project, "node": nid, "data": {"status": out["status"]}})
        return out

    def node_edit(self, args):
        project, nid = args.get("project"), args.get("node")
        exp = _int(args.get("expected_rev"), "expected_rev", 1)
        n = self._node(project, nid)
        if exp != n.get("rev"):
            raise ToolError("rev_conflict", f"node is at rev {n.get('rev')}, you sent {exp}",
                            {"current_rev": n.get("rev"), "current": n})
        try:
            new = wn.apply_edit(n, args.get("changes"), is_owner=self.by == "owner")
        except wn.GoalLocked as exc:
            raise ToolError("owner_only", str(exc))
        except wn.NodeError as exc:
            raise ToolError("bad_argument", str(exc))
        self._graph_ok(project, new)
        try:
            out = self.store.write(f"projects/{project}/nodes/{nid}.json", new, expected_rev=exp)
        except RevConflict as exc:
            raise self._conflict(exc, "node")
        self.store.append_event(self.by, {"kind": "node.edit", "summary": f"{self.by} edited task {nid}.",
                                          "project": project, "node": nid, "data": {"fields": sorted(args["changes"])}})
        return out

    @staticmethod
    def _text_arg(args, key, required):
        v = args.get(key)
        if v is None and not required:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ToolError("bad_argument", f"{key} must be non-empty text")
        return v.strip()

    @staticmethod
    def _items(raw):
        """Sub-questions answered together: [{id, prompt, kind choice|multi|scale|text, options}]."""
        if raw is None:
            return None
        if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
            raise ToolError("bad_argument", "items must be a list of 1 to 20 questions")
        out, seen = [], set()
        for it in raw:
            if not isinstance(it, dict):
                raise ToolError("bad_argument", "each item is an object {id, prompt, kind, options}")
            iid, prompt, kind = str(it.get("id") or "").strip(), it.get("prompt"), it.get("kind")
            if not iid or iid in seen or not isinstance(prompt, str) or not prompt.strip() or kind not in ITEM_KINDS:
                raise ToolError("bad_argument", f"item needs a unique id, a prompt and kind in {list(ITEM_KINDS)}")
            opts = it.get("options")
            if kind in ("choice", "multi"):
                if (not isinstance(opts, list) or not 1 <= len(opts) <= 12
                        or any(not isinstance(o, str) or not o.strip() for o in opts)):
                    raise ToolError("bad_argument", f"item {iid}: a {kind} item needs 1 to 12 options")
                opts = [o.strip() for o in opts]
            else:
                opts = None
            seen.add(iid)
            row = {"id": iid, "prompt": prompt.strip(), "kind": kind, "options": opts}
            explain = it.get("explain")
            if explain is not None:
                if not isinstance(explain, str) or not explain.strip() or len(explain) > 400:
                    raise ToolError("bad_argument", f"item {iid}: explain is one plain sentence (at most 400 characters)")
                row["explain"] = explain.strip()
            notes = BoardTools._notes(it.get("notes"), opts, f"item {iid}")
            if notes:
                row["notes"] = notes
            rec = it.get("recommended")
            if rec is not None:
                ok = (kind in ("choice", "multi") and rec in (opts or [])) or (kind == "scale" and isinstance(rec, int)
                                                                           and not isinstance(rec, bool) and 1 <= rec <= 9)
                if not ok:
                    raise ToolError("bad_argument", f"item {iid}: recommended must be one of its options (or 1-9 for a scale)")
                row["recommended"] = rec
            out.append(row)
        return out

    @staticmethod
    def _notes(raw, options, where):
        """What each choice changes: {option: one plain sentence}. Keys must be options."""
        if raw is None:
            return None
        if not isinstance(raw, dict) or not options:
            raise ToolError("bad_argument", f"{where}: notes map each option to what it changes")
        out = {}
        for k, v in raw.items():
            if k not in options or not isinstance(v, str) or not v.strip() or len(v) > 300:
                raise ToolError("bad_argument", f"{where}: notes keys must be options and values short sentences")
            out[k] = v.strip()
        return out

    @staticmethod
    def _item_answers(items, answers):
        """Checked answers by item id, and the readable lines the asking session receives."""
        if not items:
            raise ToolError("bad_argument", "this question has no items; use approve, option, reject or reply")
        if not isinstance(answers, dict) or not answers:
            raise ToolError("bad_argument", "answers must map item ids to answers (at least one)")
        by = {i["id"]: i for i in items}
        unknown = [k for k in answers if k not in by]
        if unknown:
            raise ToolError("bad_argument", f"unknown item ids: {unknown}")
        clean = {}
        for k, v in answers.items():
            it = by[k]
            if it["kind"] == "scale":
                if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 9:
                    raise ToolError("bad_argument", f"item {k}: give a whole number from 1 to 9")
            elif it["kind"] == "choice":
                if v not in it["options"]:
                    raise ToolError("bad_argument", f"item {k}: pick one of {it['options']}")
            elif it["kind"] == "multi":
                if not isinstance(v, list) or not v or any(x not in it["options"] for x in v):
                    raise ToolError("bad_argument", f"item {k}: tick one or more of {it['options']}")
            elif not isinstance(v, str) or not v.strip():
                raise ToolError("bad_argument", f"item {k}: write some text")
            clean[k] = v.strip() if isinstance(v, str) else v
        lines = []
        for it in items:
            v = clean.get(it["id"])
            shown = "(skipped)" if v is None else ", ".join(v) if isinstance(v, list) else str(v)
            lines.append(f"{it['id']}. {it['prompt']} -> {shown}")
        return clean, "\n".join(lines)

    def _new_question(self, project, nid, problem, resolution, options=None, recommended=None, session=None,
                      items=None, explain=None, notes=None):
        qid = "nq-" + secrets.token_hex(4)
        ent = {"schema": "waypoint.question.v1", "id": qid, "project": project, "node": nid,
               "kind": "items" if items else "choice" if options else "approval", "prompt": problem,
               "resolution": resolution, "items": items,
               "options": options, "recommended": recommended, "scale": None, "answer": None, "answered_at": None,
               "captured": False, "purpose": None, "session": session, "created_at": iso(self.now()),
               "created_by": self.by}
        if explain:
            ent["explain"] = explain
        if notes:
            ent["option_notes"] = notes
        return self.store.write(f"questions/{qid}.json", ent, expected_rev=0)

    def ask(self, args):
        project = _id(args.get("project"), "project")
        if not self.store.exists(f"projects/{project}/project.json"):
            raise ToolError("not_found", f"project {project} does not exist")
        nid = args.get("node")
        problem = self._text_arg(args, "problem", True)
        resolution = self._text_arg(args, "resolution", True)
        options = args.get("options")
        if options is not None:
            if (not isinstance(options, list) or not 1 <= len(options) <= 8
                    or any(not isinstance(o, str) or not o.strip() for o in options)):
                raise ToolError("bad_argument", "options must be 1 to 8 short texts")
            options = [o.strip() for o in options]
        recommended = self._text_arg(args, "recommended", False)
        if recommended is not None and options and recommended not in options:
            raise ToolError("bad_argument", "recommended must be one of the options")
        session = self._text_arg(args, "session", False)
        items = self._items(args.get("items"))
        explain = self._text_arg(args, "explain", False)
        if explain is not None and len(explain) > 400:
            raise ToolError("bad_argument", "explain is one plain sentence (at most 400 characters)")
        notes = self._notes(args.get("option_notes"), options, "option_notes")
        n = None
        if nid is not None:
            n = self._node(project, nid)
            if n["status"] in FINISHED:
                raise ToolError("bad_state", f"node is {n['status']}; a finished node needs nothing")
            c = self._live_claim(project, nid)
            if c and self.by != "owner" and args.get("token") != c.get("token"):
                raise ToolError("claimed_by_other", f"node is claimed by {c.get('claimed_by') or c.get('owner')}")
        q = self._new_question(project, nid, problem, resolution, options, recommended, session, items, explain, notes)
        link = f"questions/{q['id']}.json"
        if n is not None and n["status"] != "needs_you":
            try:
                n = self.store.write(f"projects/{project}/nodes/{nid}.json",
                                     dict(n, status="needs_you", evidence=f"asked Owner: {link}"[:500]),
                                     expected_rev=n["rev"])
            except RevConflict as exc:
                raise self._conflict(exc, "node")
        todo = self.todo_add({"text": problem, "category": "decisions", "project": project, "node": nid, "link": link,
                              "session": session})
        ev = {"kind": "question.ask", "summary": f"{self.by} asked Owner: {problem}"[:300], "project": project,
              "data": {"question": q["id"]}}
        if nid:
            ev["node"] = nid
        self.store.append_event(self.by, ev)
        return {"question": q["id"], "node": n, "todo": todo["id"]}

    def answer(self, args):
        decision = args.get("decision")
        if decision not in DECISIONS:
            raise ToolError("bad_argument", f"decision must be one of {list(DECISIONS)}")
        text = self._text_arg(args, "text", decision == "reply") if decision != "answers" else None
        qid = args.get("question")
        if qid is None:  # a needs_you task nobody wrote a question for: answer it all the same
            project = _id(args.get("project"), "project")
            nid = _id(args.get("node"), "node")
            if self._node(project, nid)["status"] != "needs_you":
                raise ToolError("bad_state", "that task is not waiting for you")
            qid = self._new_question(project, nid, NO_QUESTION, None)["id"]
        rel = f"questions/{_id(qid, 'question')}.json"
        q = self.store.read(rel)
        if not isinstance(q, dict):
            raise ToolError("not_found", f"question {qid} does not exist")
        if q.get("answered_at") or q.get("answer") is not None:
            raise ToolError("bad_state", "that question is already answered", {"answer": q.get("answer")})
        option = None
        clean = None
        tail = f". {text}" if text else ""
        if decision == "answers":
            clean, ans = self._item_answers(q.get("items"), args.get("answers"))
        elif decision == "approve":
            ans = "Approved: " + (q.get("resolution") or q.get("recommended") or "yes").rstrip() + tail
        elif decision == "option":
            option = args.get("option")
            if option not in (q.get("options") or []):
                raise ToolError("bad_argument", "option must be one of the question's options")
            ans = f"Chose: {option}{tail}"
        elif decision == "reject":
            ans = "Rejected" + (f": {text}" if text else "")
        else:
            ans = f"Reply: {text}"
        try:
            self.store.write(rel, dict(q, answer=ans, decision=decision, option=option, answers=clean,
                                       answered_at=iso(self.now()), answered_by=self.by), expected_rev=q["rev"])
        except RevConflict as exc:
            raise self._conflict(exc, "question")
        for t in self.store.list_entities("todos"):
            if t.get("status") == "open" and t.get("link") == rel:
                try:
                    self.todo_tick({"id": t["id"]})
                except ToolError:
                    pass  # a to-do changed underneath is harmless; the question is the truth
        try:
            resume.process(self.store, now=self.now, desktop=self.desktop)  # route now, not at the runner's next tick
        except Exception:  # noqa: BLE001 - routing retries on the runner's next tick
            pass
        q = self.store.read(rel)
        return {"question": q["id"], "answer": ans, "resume": q.get("resume")}

    def claim(self, args):
        project, nid = args.get("project"), args.get("node")
        self._node(project, nid)
        owner = args.get("owner", "runner")
        token = args.get("token") or secrets.token_hex(8)
        location = args.get("location")
        if not isinstance(location, str) or not location:
            raise ToolError("bad_argument", "location is required (worktree path or cloud run)")
        try:
            r = self.claims.claim(project, nid, owner, location, token, claimed_by=args.get("claimed_by") or self.by)
        except ValueError as exc:
            raise ToolError("bad_argument", str(exc))
        if not r.ok:
            h = r.holder or {}
            raise ToolError("held", "node is already claimed",
                            {"holder": {"owner": h.get("owner"), "claimed_by": h.get("claimed_by"), "since": h.get("since")}})
        return {"claimed": True, "token": token, "claim": r.claim, "reclaimed": r.reclaimed_from is not None}

    def release(self, args):
        project, nid = _id(args.get("project"), "project"), _id(args.get("node"), "node")
        token = args.get("token")
        if not isinstance(token, str):
            raise ToolError("bad_argument", "token is required")
        if not self.claims.release(project, nid, token):
            raise ToolError("not_holder", "no claim with that token on this node")
        return {"released": True}


_NODE = {"project": {"type": "string"}, "node": {"type": "string"}}
TOOLS = [
    ("board_read", "Read the board: no project lists projects; with a project gives nodes (status, rev, slot, float, claim) and open to-dos.",
     {"project": {"type": "string"}}, []),
    ("toolbox_read", "Read Owner's toolbox: tools, MCP servers, CLIs, models, routes and subscriptions.", {}, []),
    ("dashboard_read", "Read runs, open questions, usage pools and agent toggles.", {}, []),
    ("snooze", "Owner's setting from Waypoint Claude: hide one open to-do or question until a time (until: ISO) or bring it back (until null).",
     {"kind": {"enum": ["todo", "question"]}, "id": {"type": "string"}, "until": {"type": ["string", "null"]}}, ["kind", "id", "until"]),
    ("todo_add", "Add a to-do (category review, decisions or hands_on; optional project, node, link, session: the session that owns it, for Open chat).",
     {"text": {"type": "string"}, "category": {"enum": list(CATEGORY_SUBTYPE)}, "project": {"type": "string"},
      "node": {"type": "string"}, "link": {"type": "string"}}, ["text"]),
    ("todo_tick", "Tick an open to-do done. Optional expected_rev refuses a stale tick.",
     {"id": {"type": "string"}, "expected_rev": {"type": "integer"}}, ["id"]),
    ("node_status", "Set a node status. Needs expected_rev (the rev you read; stale writes are refused) and the claim token. done/skipped/carried are owner-only.",
     dict(_NODE, status={"enum": list(STATUSES)}, expected_rev={"type": "integer"}, token={"type": "string"},
          evidence={"type": "string"}), ["project", "node", "status", "expected_rev"]),
    ("node_move", "Move a node's start slot within its float (slot null = earliest). Refuses a slot outside the float.",
     dict(_NODE, slot={"type": ["integer", "null"]}, expected_rev={"type": "integer"}), ["project", "node", "slot"]),
    ("node_add", "Add a node to a project now (planning in edit mode). Needs goal and check {type: command|files_exist|text_present|human}; optional title, deps, files, estimate, kind, lane, effort, recommended, parent, status, evidence. done/skipped/carried are owner-only.",
     dict(_NODE, goal={"type": "string"}, check={"type": "object"}, title={"type": "string"}, deps={"type": "array"},
          files={"type": "array", "items": {"type": "string"}}, estimate={"type": "number"}, kind={"type": "string"},
          lane={"type": "string"}, effort={"type": "string"}, recommended={"type": "string"}, parent={"type": "string"},
          status={"enum": list(STATUSES)}, evidence={"type": "string"}), ["project", "node", "goal", "check"]),
    ("node_edit", "Edit a node in place: changes is an object of title, goal (owner only), check (a non-owner change resets the node to not_started), deps, files, estimate, kind, lane, effort, recommended, parent. Needs expected_rev.",
     dict(_NODE, expected_rev={"type": "integer"}, changes={"type": "object"}), ["project", "node", "expected_rev", "changes"]),
    ("ask", "Ask Owner (sets the node needs_you). Write for Owner in plain English (ASD-STE100): no internal names, file paths or jargon. Every ask carries problem (what blocks, one or two plain sentences), resolution (what you propose), explain (one plain sentence: what the question means and why it matters), optional options (1-8, short plain labels) with option_notes ({option: what choosing it changes}) and recommended (one of them, only for a choice he delegated), optional items (several questions answered at once: [{id, prompt, kind choice|multi|scale|text, options, explain, notes {option: what it changes}, recommended}]; he may skip any item; never point Owner at a file of questions), optional node, session (your session id, so the answer comes back to you and Open chat finds you) and token (your claim).",
     {"project": {"type": "string"}, "node": {"type": "string"}, "problem": {"type": "string"},
      "resolution": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
      "recommended": {"type": "string"}, "session": {"type": "string"}, "token": {"type": "string"},
      "items": {"type": "array", "items": {"type": "object"}}, "explain": {"type": "string"},
      "option_notes": {"type": "object"}},
     ["project", "problem", "resolution"]),
    ("answer", "Answer a needs-you question: decision approve (the proposed resolution), option (with option), reject (optional text), reply (text), or answers ({item id: answer} for a question with items). Without question, give project and node of a needs_you task. The answer is routed to the owning session at once.",
     {"question": {"type": "string"}, "decision": {"enum": list(DECISIONS)}, "option": {"type": "string"},
      "text": {"type": "string"}, "project": {"type": "string"}, "node": {"type": "string"},
      "answers": {"type": "object"}}, ["decision"]),
    ("claim", "Claim a node (shared claim protocol). Returns the token you need for node_status and release.",
     dict(_NODE, location={"type": "string"}, owner={"type": "string"}, claimed_by={"type": "string"}, token={"type": "string"}),
     ["project", "node", "location"]),
    ("release", "Release your claim on a node with its token.", dict(_NODE, token={"type": "string"}), ["project", "node", "token"]),
]


READ_ONLY = {"board_read", "toolbox_read", "dashboard_read"}  # MCP hint: hosts may run these without asking the viewer to confirm each call


def tools_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r},
             "annotations": {"readOnlyHint": n in READ_ONLY}}
            for n, d, p, r in TOOLS]


def call_tool(board, name, args):
    if name not in {t[0] for t in TOOLS}:
        raise ToolError("unknown_tool", f"Unknown tool: {name}")
    return getattr(board, name)(args if isinstance(args, dict) else {})


def _text(obj, is_error):
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": is_error}


def handle(board, message):
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "waypoint-board", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools_list()}
    elif method == "tools/call":
        try:
            result = _text(call_tool(board, str(params.get("name", "")), params.get("arguments") or {}), False)
        except ToolError as exc:
            result = _text({"error": exc.code, "message": str(exc), **exc.data}, True)
        except Exception as exc:  # noqa: BLE001
            result = _text({"error": "internal", "message": str(exc)}, True)
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main(data_root=None, by=None, stdin=None, stdout=None):
    root = data_root or os.environ.get("WAYPOINT_DATA_DIR")
    if not root:
        sys.stderr.write("WAYPOINT_DATA_DIR is not set\n")
        return 2
    board = BoardTools(root, by=by or os.environ.get("WAYPOINT_MCP_BY") or "agent")
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = handle(board, message) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if response is not None:
            stdout.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
