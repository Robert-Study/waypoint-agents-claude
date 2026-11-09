"""Brain index: a read-only knowledge graph over Owner's notes, skills,
instructions and a shallow folder tree.

Sources (under AI_DRIVE_ROOT, default from cc_engine.config):
  notes/06 System & Governance/Preferences/*.md      kind=preference
  notes/06 System & Governance/Brain/**/*.md         kind=brain
  notes/07 Skills Registry/*/SKILL.md                kind=skill
  notes/AGENTS.md, .../Agent Roles/*.md, the two
  brain/preference INDEX.md files and Brain/CONTRACT.md    kind=instruction
  depth-2 folder trees of notes and AI_DISK_ROOT     kind=folder

The sources are never written. The only file this module writes is the
generated cache (``data/brain_index.json`` by default), which is never
hand-edited. Python standard library only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from . import config

DEFAULT_DRIVE_ROOT = config.DRIVE_ROOT
DEFAULT_DISK_ROOT = config.WORKSPACE_ROOT
SCHEMA_VERSION = 1

EXCHANGE = config.EXCHANGE_DIR
GOV = "06 System & Governance"
SKILLS = "07 Skills Registry"

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "data", "__pycache__", ".pytest_cache"}
# Generated compilations that duplicate the notes; indexing them would double count.
SKIP_PREF_FILES = {"INDEX.md", "USER_PREFERENCES_AND_PHILOSOPHY.md"}

KINDS = ("preference", "brain", "skill", "instruction", "folder")
LINK_KINDS = ("link", "related", "supersedes", "contains", "cites")

# Provenance -> brightness (explicit > reported calibration > inherited).
BRIGHTNESS = {
    "explicit_user_statement": 1.0,
    "user_reported_calibration": 0.7,
    "vetted_source": 0.6,
    "agent_synthesis": 0.5,
    "inherited_registry": 0.35,
}
UNKNOWN_BRIGHTNESS = 0.3
PROVENANCE_RANK = {k: i for i, k in enumerate(
    ["explicit_user_statement", "user_reported_calibration", "vetted_source",
     "agent_synthesis", "inherited_registry"])}

# Preference clusters by tag family (03_ux_direction.md section 7).
CLUSTERS = {
    "UI & design": {
        "ui", "design", "html", "colour", "graph", "preview", "artifacts", "opening-view",
        "editing", "likert-scale", "interactive", "anti-slop", "visual", "star-map"},
    "Models & budget": {
        "models", "budget", "routing", "quota-reset", "quota-floor", "tokens", "claude", "codex",
        "antigravity", "calibration", "float", "cache-ttl", "caching", "context-budget",
        "modelling", "plain-names", "naming", "efficiency", "quota"},
    "Orchestration": {
        "orchestration", "subagents", "delegation", "automation", "overnight", "autonomy",
        "retries", "resilience", "workers", "parallelism", "orchestrator-plus", "outsourcing",
        "execution", "error-handling", "prompts", "verification", "review", "handoff",
        "manual-dispatch", "sprints", "scheduling", "dependencies", "capacity", "durability",
        "safety", "authorization", "roles"},
    "Dashboard & workflow": {
        "dashboard", "workflow", "notifications", "controls", "cli", "priorities",
        "iteration-three", "build-order", "specification", "summaries", "learnharness", "dsh",
        "planning", "performance"},
    "Knowledge & filing": {
        "filing", "knowledge", "provenance", "navigation", "workspace", "drive", "index",
        "obsidian", "brain", "capture", "evidence", "worktrees", "git", "github", "backup",
        "cloud", "local-execution", "data", "instructions", "instruction-hygiene", "context",
        "retirement", "development"},
    "Communication": {
        "communication", "question-bombing", "grilling", "choice", "recommendations",
        "decisions", "human", "simplicity", "compact", "assumptions", "certainty", "discovery",
        "research", "exploration", "alignment", "log", "continuous"},
    "Projects & delivery": {
        "portfolio", "tables", "learning", "release", "publishing",
        "publication", "delivery", "forecasting", "evaluation"},
}
CLUSTER_ORDER = list(CLUSTERS)
DEFAULT_CLUSTER = "Orchestration"

LINK_PRIORITY = {"supersedes": 0, "related": 1, "contains": 2, "link": 3, "cites": 4}

_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\n]+)\)")
_WIKI_LINK = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
_SENTENCE = re.compile(r"[^.!?\n]*supersed[^.!?\n]*[.!?]?", re.I)
_NOTE_NUM = re.compile(r"\bnotes?\s+(\d{1,3})\b", re.I)
_SKILL_PATH = re.compile(r"Skills Registry[\\/]+([A-Za-z0-9_.-]+)", re.I)
_SKILL_FILE = re.compile(r"([A-Za-z0-9_-]+)[\\/]SKILL\.md", re.I)
_SKILL_TICK = re.compile(r"`([a-z0-9][a-z0-9-]+)`\s+skill", re.I)


# ---------------------------------------------------------------- roots/paths
def drive_root() -> Path:
    return Path(os.environ.get("AI_DRIVE_ROOT") or DEFAULT_DRIVE_ROOT)


def disk_root() -> Path:
    return Path(os.environ.get("AI_DISK_ROOT") or DEFAULT_DISK_ROOT)


def data_dir() -> Path:
    return config.data_dir()


def default_cache_path() -> Path:
    return data_dir() / "brain_index.json"


def _key(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


# ---------------------------------------------------------------- parsing
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _parse_scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_parse_scalar(x) for x in inner.split(",")] if inner else []
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _mini_yaml(block: str) -> dict:
    """Small YAML subset: ``key: value``, ``key: [a, b]`` and ``key:`` + ``- item`` lists."""
    out: dict = {}
    current = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*-\s+(.*)$", raw)
        if m and current is not None:
            if not isinstance(out.get(current), list):
                out[current] = []
            out[current].append(_parse_scalar(m.group(1)))
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", raw)
        if m:
            current = m.group(1)
            val = m.group(2)
            out[current] = _parse_scalar(val) if val.strip() else []
    return out


def parse_frontmatter(text: str):
    """Return (frontmatter dict, body). Accepts JSON or simple YAML between ``---`` lines."""
    m = re.match(r"^\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text
    block, body = m.group(1), text[m.end():]
    fm = None
    if block.lstrip().startswith("{"):
        try:
            fm = json.loads(block)
        except ValueError:
            fm = None
    if not isinstance(fm, dict):
        fm = _mini_yaml(block)
    return fm, body


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _title(fm, body, fallback):
    if fm.get("title"):
        return str(fm["title"])
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else fallback


def _summary(body: str, limit=240) -> str:
    m = re.search(r"^##\s+Core Idea\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    chunk = m.group(1) if m else body
    for para in re.split(r"\n\s*\n", chunk):
        p = " ".join(para.strip().split())
        if p and not p.startswith("#") and not p.startswith("---"):
            return p if len(p) <= limit else p[: limit - 1].rstrip() + "\u2026"
    return ""


def cluster_for(tags, ident="", tag_counts=None) -> str:
    """Pick the tag family with the highest score; rarer (more specific) tags weigh more."""
    tokens = [t.lower() for t in tags if t.lower() != "preferences"]
    scores = {c: 0.0 for c in CLUSTER_ORDER}
    for t in tokens:
        w = 1.0 / max(1, (tag_counts or {}).get(t, 1)) ** 0.5
        for c, family in CLUSTERS.items():
            if t in family:
                scores[c] += w
    if not any(scores.values()):
        for t in re.split(r"[_\-\s]+", ident.lower()):
            for c, family in CLUSTERS.items():
                if t in family:
                    scores[c] += 0.5
    best = max(CLUSTER_ORDER, key=lambda c: (scores[c], -CLUSTER_ORDER.index(c)))
    return best if scores[best] > 0 else DEFAULT_CLUSTER


def brightness_for(provenance) -> float:
    return BRIGHTNESS.get(provenance or "", UNKNOWN_BRIGHTNESS)


# ---------------------------------------------------------------- builder
class _Builder:
    def __init__(self, drive: Path, disk: Path):
        self.drive = Path(drive)
        self.disk = Path(disk)
        self.exchange = self.drive / EXCHANGE
        self.gov = self.exchange / GOV
        self.nodes: dict = {}
        self.texts: dict = {}          # node id -> (body text, source dir)
        self.by_path: dict = {}        # normcased abs path -> node id
        self.by_name: dict = {}        # lower id/stem/title -> node id
        self.folders_by_path: dict = {}
        self.links: dict = {}          # (src, dst) -> kind
        self.unresolved = 0
        self.tag_counts: dict = {}

    # ---- nodes
    def add(self, nid, label, kind, cluster, path, provenance=None, weight=1, **extra):
        node = {"id": nid, "label": label, "kind": kind, "cluster": cluster,
                "path": str(path), "provenance": provenance, "weight": int(max(1, weight)),
                "brightness": brightness_for(provenance) if kind in ("preference", "brain") else None}
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        self.nodes[nid] = node
        self.by_path[_key(path)] = nid
        return node

    def _names(self, nid, *names):
        for n in names:
            if n:
                self.by_name.setdefault(str(n).strip().lower(), nid)

    def note(self, path: Path, kind: str, cluster_hint=None):
        text = _read(path)
        fm, body = parse_frontmatter(text)
        ident = str(fm.get("id") or path.stem)
        nid = ("pref:" if kind == "preference" else "brain:") + ident
        tags = _as_list(fm.get("tags"))
        provenance = fm.get("evidence_status") or fm.get("provenance") or None
        sources = _as_list(fm.get("source_ids"))
        cluster = cluster_for(tags, ident, self.tag_counts) if kind == "preference" else (cluster_hint or "Brain")
        node = self.add(
            nid, _title(fm, body, path.stem), kind, cluster, path, provenance,
            weight=len(sources) or 1, tags=[t for t in tags if t != "preferences"],
            note_kind=fm.get("kind"), scope=fm.get("scope"),
            captured_at=fm.get("captured_at"), updated_at=fm.get("updated_at"),
            summary=_summary(body))
        node["_related"] = _as_list(fm.get("related"))
        node["_supersedes"] = _as_list(fm.get("supersedes"))
        node["_superseded_by"] = _as_list(fm.get("superseded_by"))
        self.texts[nid] = (body, path.parent)
        self._names(nid, ident, path.stem, node["label"])
        return node

    def collect(self):
        prefs = self.gov / "Preferences"
        if prefs.is_dir():
            pref_files = [p for p in sorted(prefs.glob("*.md")) if p.name not in SKIP_PREF_FILES]
            for p in pref_files:
                for t in _as_list(parse_frontmatter(_read(p))[0].get("tags")):
                    self.tag_counts[t.lower()] = self.tag_counts.get(t.lower(), 0) + 1
            for p in sorted(prefs.glob("*.md")):
                if p.name not in SKIP_PREF_FILES:
                    self.note(p, "preference")
        brain = self.gov / "Brain"
        if brain.is_dir():
            for p in sorted(brain.rglob("*.md")):
                if p.parent == brain:
                    continue  # INDEX/CONTRACT are instructions, below
                rel = p.relative_to(brain).parts
                self.note(p, "brain", cluster_hint=rel[0] if len(rel) > 1 else "Brain")
        skills = self.exchange / SKILLS
        if skills.is_dir():
            for d in sorted(x for x in skills.iterdir() if x.is_dir()):
                sk = d / "SKILL.md"
                if not sk.is_file():
                    continue
                text = _read(sk)
                fm, body = parse_frontmatter(text)
                name = str(fm.get("name") or d.name)
                nid = "skill:" + d.name
                self.add(nid, name, "skill", "Skills", sk,
                         summary=str(fm.get("description") or "")[:240] or _summary(body))
                self.texts[nid] = (body, sk.parent)
                self._names(nid, d.name, name)
        instr = []
        if (self.exchange / "AGENTS.md").is_file():
            instr.append(self.exchange / "AGENTS.md")
        roles = self.gov / "Agent Roles"
        if roles.is_dir():
            instr.extend(sorted(roles.glob("*.md")))
        for extra in (prefs / "INDEX.md", brain / "INDEX.md", brain / "CONTRACT.md"):
            if extra.is_file():
                instr.append(extra)
        for p in instr:
            text = _read(p)
            fm, body = parse_frontmatter(text)
            rel = p.relative_to(self.exchange).as_posix()
            nid = "instr:" + rel
            label = p.name if p.parent in (self.exchange, roles) else f"{p.parent.name}/{p.name}"
            self.add(nid, label, "instruction", "Instructions", p, summary=_summary(body))
            self.texts[nid] = (body, p.parent)
            self._names(nid, rel, label)
        self.tree(self.exchange, EXCHANGE)
        self.tree(self.disk, self.disk.name or "workspace")

    def tree(self, root: Path, label: str, max_depth=2):
        if not root.is_dir():
            return

        def visit(path: Path, rel: str, depth: int, parent):
            nid = "folder:" + label + ("/" + rel if rel else "")
            self.add(nid, path.name if rel else label, "folder", label, path, depth=depth)
            self.folders_by_path[_key(path)] = nid
            if parent:
                self.link(parent, nid, "contains")
            if depth >= max_depth:
                return
            try:
                entries = sorted((e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)),
                                 key=lambda e: e.name.lower())
            except OSError:
                return
            for e in entries:
                if e.name in SKIP_DIRS or e.name.startswith("."):
                    continue
                visit(Path(e.path), (rel + "/" if rel else "") + e.name, depth + 1, nid)

        visit(root, "", 0, None)

    # ---- links
    def link(self, src, dst, kind):
        if src == dst or src not in self.nodes or dst not in self.nodes:
            return
        pair = (src, dst)
        old = self.links.get(pair)
        if old is None or LINK_PRIORITY[kind] < LINK_PRIORITY[old]:
            # A related/supersedes link already covers the reverse pair: skip a plain duplicate.
            rev = self.links.get((dst, src))
            if kind in ("link", "cites") and rev in ("related", "supersedes"):
                return
            self.links[pair] = kind

    def resolve_name(self, ref: str, prefer_kind=None):
        ref = ref.strip()
        if not ref:
            return None
        if ref.startswith("pref:") or ref.startswith("brain:") or ref.startswith("skill:"):
            if ref in self.nodes:
                return ref
            ref = ref.split(":", 1)[1]
        low = ref.lower()
        if prefer_kind:
            cand = f"{prefer_kind}:{ref}"
            if cand in self.nodes:
                return cand
        for prefix in ("pref:", "brain:", "skill:"):
            if prefix + ref in self.nodes:
                return prefix + ref
        if low.endswith(".md"):
            low = low[:-3]
        return self.by_name.get(low)

    def resolve_path(self, target: str, base: Path):
        t = unquote(target.strip().strip("<>")).split("#", 1)[0].split("?", 1)[0]
        if not t or re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) and not re.match(r"^[a-z]:[\\/]", t, re.I):
            return None  # URL (http:, mailto:, ...), not a file
        p = Path(t) if os.path.isabs(t) else base / t
        nid = self.by_path.get(_key(p))
        if nid:
            return nid
        return self.folders_by_path.get(_key(p))

    def _container(self, path: Path):
        cur = Path(path).parent
        for _ in range(64):
            nid = self.folders_by_path.get(_key(cur))
            if nid:
                return nid
            if cur.parent == cur:
                return None
            cur = cur.parent
        return None

    def _pref_by_number(self, num: str):
        hits = [nid for nid in self.nodes if nid.startswith("pref:") and
                re.match(rf"^pref:0*{int(num)}_", nid)]
        return hits[0] if len(hits) == 1 else None

    def build_links(self):
        for nid, node in list(self.nodes.items()):
            if node["kind"] == "folder":
                continue
            parent = self._container(Path(node["path"]))
            if parent:
                self.link(parent, nid, "contains")
        for nid, node in self.nodes.items():
            if node["kind"] not in ("preference", "brain"):
                continue
            pk = "pref" if node["kind"] == "preference" else "brain"
            for ref in node.get("_supersedes", []):
                tgt = self.resolve_name(ref, pk)
                self.link(nid, tgt, "supersedes") if tgt else self._miss()
            for ref in node.get("_superseded_by", []):
                tgt = self.resolve_name(ref, pk)
                self.link(tgt, nid, "supersedes") if tgt else self._miss()
            for ref in node.get("_related", []):
                tgt = self.resolve_name(ref, pk)
                self.link(nid, tgt, "related") if tgt else self._miss()
            body = self.texts.get(nid, ("", None))[0]
            if node["kind"] == "preference":
                for sent in _SENTENCE.findall(body):
                    for num in _NOTE_NUM.findall(sent):
                        other = self._pref_by_number(num)
                        if not other or other == nid:
                            continue
                        if re.search(r"superseded\s+by", sent, re.I):
                            self.link(other, nid, "supersedes")
                        else:
                            self.link(nid, other, "supersedes")
        for nid, (body, base) in self.texts.items():
            for _, target in _MD_LINK.findall(body):
                tgt = self.resolve_path(target, base)
                if tgt:
                    self.link(nid, tgt, "link")
                elif target.lower().split("#")[0].endswith(".md"):
                    self._miss()
            for ref in _WIKI_LINK.findall(body):
                tgt = self.resolve_name(ref)
                self.link(nid, tgt, "link") if tgt else self._miss()
        self.build_cites()

    def _miss(self):
        self.unresolved += 1

    def build_cites(self):
        note_ids = {}
        for nid, node in self.nodes.items():
            if node["kind"] in ("preference", "brain"):
                ident = nid.split(":", 1)[1]
                if len(ident) >= 6:
                    note_ids[ident] = nid
        note_re = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(
            re.escape(k) for k in sorted(note_ids, key=len, reverse=True)) + r")(?![A-Za-z0-9_-])") \
            if note_ids else None
        instr_keys = {}
        for nid, node in self.nodes.items():
            if node["kind"] == "instruction":
                p = Path(node["path"])
                if p.name.upper() in ("INDEX.MD", "CONTRACT.MD"):
                    instr_keys[nid] = re.compile(re.escape(p.parent.name) + r"[\\/]+" + re.escape(p.name), re.I)
                else:
                    instr_keys[nid] = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(p.name), re.I)
        for nid, (body, _base) in self.texts.items():
            kind = self.nodes[nid]["kind"]
            names = set(_SKILL_PATH.findall(body)) | set(_SKILL_FILE.findall(body)) | set(_SKILL_TICK.findall(body))
            for name in names:
                tgt = "skill:" + name.strip().rstrip(".")
                if tgt in self.nodes:
                    self.link(nid, tgt, "cites")
            if kind not in ("skill", "instruction"):
                continue
            if note_re:
                for ident in set(note_re.findall(body)):
                    self.link(nid, note_ids[ident], "cites")
            for tgt, rx in instr_keys.items():
                if rx.search(body):
                    self.link(nid, tgt, "cites")

    def result(self):
        nodes = []
        for node in self.nodes.values():
            nodes.append({k: v for k, v in node.items() if not k.startswith("_")})
        links = [{"source": s, "target": t, "kind": k} for (s, t), k in self.links.items()]
        links.sort(key=lambda l: (LINK_KINDS.index(l["kind"]), l["source"], l["target"]))
        counts = {k: 0 for k in KINDS}
        for n in nodes:
            counts[n["kind"]] += 1
        link_counts = {k: 0 for k in LINK_KINDS}
        for l in links:
            link_counts[l["kind"]] += 1
        clusters = {}
        for n in nodes:
            if n["kind"] == "preference":
                clusters[n["cluster"]] = clusters.get(n["cluster"], 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "generated": True,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "roots": {"drive": str(self.drive), "disk": str(self.disk)},
            "counts": counts,
            "link_counts": link_counts,
            "preference_clusters": clusters,
            "unresolved_links": self.unresolved,
            "provenance_order": list(PROVENANCE_RANK),
            "nodes": nodes,
            "links": links,
        }


def _guard_cache_path(cache: Path, drive: Path, disk: Path):
    """The cache must never land inside the indexed note sources."""
    ck = _key(cache)
    for src in (drive / EXCHANGE,):
        sk = _key(src)
        if ck == sk or ck.startswith(sk + os.sep):
            raise ValueError(f"refusing to write brain cache inside sources: {cache}")


def _write_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".brain_index.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def build_graph(drive_root_path=None, disk_root_path=None, cache_path=None, write_cache=True) -> dict:
    """Build the knowledge graph. Reads sources only; writes only the cache file."""
    drive = Path(drive_root_path) if drive_root_path else drive_root()
    disk = Path(disk_root_path) if disk_root_path else disk_root()
    b = _Builder(drive, disk)
    b.collect()
    b.build_links()
    graph = b.result()
    if write_cache:
        cache = Path(cache_path) if cache_path else default_cache_path()
        _guard_cache_path(cache, drive, disk)
        _write_atomic(cache, graph)
    return graph


def load_cached(cache_path=None):
    cache = Path(cache_path) if cache_path else default_cache_path()
    try:
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION else None
    except (OSError, ValueError):
        return None


def get_graph(refresh=False, max_age_seconds=600, cache_path=None, drive_root_path=None,
              disk_root_path=None) -> dict:
    """Return the cached graph when fresh and for the same roots, else rebuild it.
    Intended for GET /api/brain (``?refresh=1`` -> refresh=True)."""
    drive = str(Path(drive_root_path) if drive_root_path else drive_root())
    disk = str(Path(disk_root_path) if disk_root_path else disk_root())
    if not refresh:
        cached = load_cached(cache_path)
        if cached and cached.get("roots") == {"drive": drive, "disk": disk}:
            try:
                built = datetime.fromisoformat(cached["built_at"])
                age = (datetime.now(timezone.utc) - built).total_seconds()
            except (KeyError, ValueError):
                age = None
            if age is not None and 0 <= age <= max_age_seconds:
                cached["cached"] = True
                return cached
    return build_graph(drive, disk, cache_path=cache_path)


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Build the brain index (read-only over sources).")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache")
    a = ap.parse_args(argv)
    g = build_graph(cache_path=a.cache, write_cache=not a.no_cache)
    print(json.dumps({"counts": g["counts"], "link_counts": g["link_counts"],
                      "preference_clusters": g["preference_clusters"],
                      "unresolved_links": g["unresolved_links"]}, indent=1))


if __name__ == "__main__":
    _main()
