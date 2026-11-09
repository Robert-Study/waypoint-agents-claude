"""Brain overlaps: find duplicate material across the notes the brain index covers.

Three finding types, over Preferences notes, Brain notes, skills and instruction files
(the same markdown sources as ``brain_index``; folders are not read):

  near_duplicate  two notes whose normalised bodies share most of their word 5-grams
                  (Jaccard, or containment of the shorter note in the longer one)
  passage         a paragraph of >= 40 words repeated (exactly or nearly) in >= 2 notes
  superseded      a note that another note supersedes (frontmatter or text mention) but
                  that still carries a full body, or two notes with the same title stem

A preference restated by a skill or instruction is intended (skills and instructions
should reflect preferences), so those pairs are never reported.

Read-only over sources: nothing here edits, moves or deletes a note; each finding carries
a one-sentence suggestion and Owner decides. The only file written is the generated cache
``data/brain_overlaps.json`` (with source mtimes, so it is reused until a note changes).
Python standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import brain_index as bi

SCHEMA_VERSION = 1
ALGO_VERSION = 1          # bump when thresholds/normalisation change -> cache rebuilds

SHINGLE = 5               # words per shingle
MIN_DOC_WORDS = 60        # notes shorter than this are never near-duplicate candidates
NEAR_JACCARD = 0.5        # whole-note Jaccard that counts as a near-duplicate
NEAR_CONTAIN = 0.8        # ... or the shorter note this much contained in the longer one
NEAR_MIN_SHARED = 40      # ... with at least this many shared shingles
PASSAGE_MIN_WORDS = 40    # paragraph size for duplicated passages
PASSAGE_CONTAIN = 0.8     # shorter paragraph's shingles found in the longer one
SUPERSEDED_MIN_WORDS = 40  # a superseded note already reduced to a stub is not reported
EXCERPT_CHARS = 300
BOILERPLATE_DF = 6        # a shingle in this many notes is template text, not shared material
BOILERPLATE_SHARE = 0.03  # ... or in this share of all notes, whichever is larger
COMMON_DF = 30            # shingles in more notes than this are skipped for candidate search
METADATA_SECTIONS = ("source", "sources", "provenance", "hashtags", "related notes", "related",
                     "human equivalent", "evidence")

KIND_RANK = {"preference": 0, "brain": 1, "instruction": 2, "skill": 3}
INTENDED = {"skill", "instruction"}   # may restate preferences by design
GENERIC_STEMS = {"index", "contract", "readme", "skill", "agents", "notes", "note", "draft", "todo"}

ROUTE = "GET /api/brain/overlaps"

_LOCK = threading.Lock()

_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_WIKI = re.compile(r"\[\[([^\]|\n]+)(?:\|([^\]\n]*))?\]\]")
_HTML = re.compile(r"<[^>\n]{1,200}>")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SECTION = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_EMPHASIS = re.compile(r"\*\*|__|`")
_HEADING_LINE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t].*$")
_STEM_NUM =re.compile(r"^\d{1,3}[_\- ]+")
_STEM_TAIL = re.compile(r"([_\- ]+(v\d+|old|new|copy|draft|final|archive|backup|\d{4}-\d{2}-\d{2}|\d{8}))+$")
_STEM_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}[_\- ]+")


# ---------------------------------------------------------------- paths/cache
def default_cache_path() -> Path:
    return bi.data_dir() / "brain_overlaps.json"


def source_paths(drive: Path) -> list:
    """The markdown files brain_index reads as notes (same globs, no folders)."""
    ex = Path(drive) / bi.EXCHANGE
    gov = ex / bi.GOV
    out = []
    prefs = gov / "Preferences"
    if prefs.is_dir():
        out += [p for p in sorted(prefs.glob("*.md")) if p.name not in bi.SKIP_PREF_FILES]
    brain = gov / "Brain"
    if brain.is_dir():
        out += [p for p in sorted(brain.rglob("*.md")) if p.parent != brain]
    skills = ex / bi.SKILLS
    if skills.is_dir():
        out += [d / "SKILL.md" for d in sorted(x for x in skills.iterdir() if x.is_dir())
                if (d / "SKILL.md").is_file()]
    if (ex / "AGENTS.md").is_file():
        out.append(ex / "AGENTS.md")
    roles = gov / "Agent Roles"
    if roles.is_dir():
        out += sorted(roles.glob("*.md"))
    for extra in (prefs / "INDEX.md", brain / "INDEX.md", brain / "CONTRACT.md"):
        if extra.is_file():
            out.append(extra)
    return out


def source_mtimes(drive: Path) -> dict:
    out = {}
    for p in source_paths(drive):
        try:
            out[str(p)] = os.stat(p).st_mtime_ns
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- text
def _strip_markup(text: str) -> str:
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _WIKI.sub(lambda m: m.group(2) or m.group(1), text)
    return _HTML.sub(" ", text)


def words(text: str) -> list:
    return _WORD.findall(_strip_markup(text).lower())


def shingles(tokens, k=SHINGLE) -> set:
    if len(tokens) < k:
        return {hash(" ".join(tokens))} if tokens else set()
    return {hash(" ".join(tokens[i:i + k])) for i in range(len(tokens) - k + 1)}


def spans(text: str):
    """(markup-stripped text, [(start, end)] word spans) so a word run maps back to prose."""
    t = _strip_markup(text)
    low = t.lower()
    if len(low) != len(t):
        t = low  # rare case-folding that changes length: excerpt in lower case instead
    return t, [(m.start(), m.end()) for m in _WORD.finditer(low)]


def shared_runs(tokens, other: set, min_words=PASSAGE_MIN_WORDS, gap=SHINGLE, k=SHINGLE) -> list:
    """Runs of consecutive word positions whose shingles also occur in ``other``.

    A one-word edit breaks ``k`` shingles, so misses up to ``gap`` long are bridged.
    Returns [(first word, end word exclusive)] for runs of at least ``min_words`` words.
    """
    hits = [i for i in range(len(tokens) - k + 1) if hash(" ".join(tokens[i:i + k])) in other]
    runs, start, last = [], None, None
    for i in hits:
        if start is None:
            start = last = i
        elif i - last <= gap + 1:
            last = i
        else:
            runs.append((start, last + k))
            start = last = i
    if start is not None:
        runs.append((start, last + k))
    return [r for r in runs if r[1] - r[0] >= min_words]


def paragraphs(body: str) -> list:
    """Blank-line separated blocks, headings and fence markers dropped; code kept as text."""
    out, cur = [], []

    def flush():
        if cur:
            raw = "\n".join(cur).strip()
            if raw:
                out.append(raw)
            cur.clear()

    for line in body.splitlines():
        s = line.strip()
        if not s or _FENCE.match(line) or re.fullmatch(r"[-*_=|: ]{3,}", s):
            flush()
            continue
        if s.startswith("#"):
            flush()
            continue
        cur.append(line)
    flush()
    return out


def content(body: str) -> str:
    """Body without generated metadata sections (Source, Provenance, Hashtags, Related...)."""
    out, skip_level = [], 0
    for line in body.splitlines():
        m = _SECTION.match(line)
        if m:
            level = len(m.group(1))
            if skip_level and level > skip_level:
                continue
            skip_level = level if m.group(2).strip().lower().rstrip(":") in METADATA_SECTIONS else 0
        if not skip_level:
            out.append(line)
    return "\n".join(out)


def excerpt(text: str, limit=EXCERPT_CHARS) -> str:
    t = " ".join(_EMPHASIS.sub("", _strip_markup(text)).split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def stem_key(name: str) -> str:
    s = name.lower()
    if s.endswith(".md"):
        s = s[:-3]
    s = _STEM_DATE.sub("", _STEM_NUM.sub("", s))
    s = _STEM_TAIL.sub("", s)
    return " ".join(re.split(r"[_\-\s]+", s)).strip()


# ---------------------------------------------------------------- documents
class _NotesOnly(bi._Builder):
    """brain_index's collector without the folder trees (ids stay identical)."""

    def tree(self, root, label, max_depth=2):
        return None


class Doc:
    __slots__ = ("id", "path", "title", "kind", "provenance", "mtime", "body", "text", "spans",
                 "tokens", "sh", "core", "paras", "inbound", "updated")

    def as_ref(self, role=None):
        out = {"id": self.id, "path": self.path, "title": self.title, "kind": self.kind,
               "name": display_name(self)}
        if role:
            out["role"] = role
        return out


def display_name(d) -> str:
    p = Path(d.path)
    if d.kind == "skill":
        return p.parent.name + "/SKILL.md"
    if p.name.upper() in ("INDEX.MD", "CONTRACT.MD"):
        return p.parent.name + "/" + p.name
    return p.name


def boilerplate_df(n_docs: int) -> int:
    return max(BOILERPLATE_DF, int(round(BOILERPLATE_SHARE * n_docs)))


def _docs(builder) -> dict:
    inbound = {}
    for (src, dst), kind in builder.links.items():
        if kind != "contains":
            inbound[dst] = inbound.get(dst, 0) + 1
    docs = {}
    for nid, node in builder.nodes.items():
        if node["kind"] == "folder" or nid not in builder.texts:
            continue
        d = Doc()
        d.id, d.path, d.title, d.kind = nid, node["path"], node["label"], node["kind"]
        d.provenance = node.get("provenance")
        try:
            d.mtime = os.stat(d.path).st_mtime
        except OSError:
            d.mtime = 0.0
        d.updated = str(node.get("updated_at") or node.get("captured_at") or "")
        d.body = content(builder.texts[nid][0])
        d.text, d.spans = spans(d.body)
        low = d.text.lower()
        d.tokens = [low[a:b] for a, b in d.spans]
        d.sh = shingles(d.tokens)
        d.paras = []
        for raw in paragraphs(d.body):
            toks = words(raw)
            if len(toks) >= PASSAGE_MIN_WORDS:
                d.paras.append({"raw": raw, "n": len(toks), "key": " ".join(toks), "sh": shingles(toks)})
        d.inbound = inbound.get(nid, 0)
        docs[nid] = d
    # Template text (the same sentence in many generated notes) is not shared material.
    df = {}
    for d in docs.values():
        for s in d.sh:
            df[s] = df.get(s, 0) + 1
    cap = boilerplate_df(len(docs))
    common = {s for s, c in df.items() if c >= cap}
    for d in docs.values():
        d.core = d.sh - common
    return docs


def _intended(a, b) -> bool:
    """A skill/instruction restating a preference is by design, never a finding."""
    return (a.kind in INTENDED and b.kind == "preference") or (b.kind in INTENDED and a.kind == "preference")


def _keeper_key(d):
    prov = bi.PROVENANCE_RANK.get(d.provenance or "", 9)
    return (prov, KIND_RANK.get(d.kind, 9), -d.inbound, d.mtime, d.path)


def _names(refs, limit=2) -> str:
    names = [display_name(r) for r in refs]
    if len(names) <= limit:
        return " and ".join(names)
    rest = len(names) - limit
    return ", ".join(names[:limit]) + f" and {rest} other" + ("s" if rest > 1 else "")


def _fid(*parts) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- finders
def _pair_counts(sets: dict, cap: int) -> dict:
    """(a, b) -> shared element count via an inverted index; very common elements skipped."""
    index = {}
    for key in sorted(sets):
        for s in sets[key]:
            index.setdefault(s, []).append(key)
    counts = {}
    for ids in index.values():
        if len(ids) < 2 or len(ids) > cap:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                k = (ids[i], ids[j])
                counts[k] = counts.get(k, 0) + 1
    return counts


def _best_shared_paragraph(src, other):
    best, best_n, score = None, 0, 0.0
    for p in paragraphs(src.body):
        toks = words(p)
        if len(toks) < 12:
            continue
        sh = shingles(toks)
        c = len(sh & other.core) / max(1, len(sh))
        if c > score + 1e-9 or (abs(c - score) < 1e-9 and len(toks) > best_n):
            best, best_n, score = p, len(toks), c
    return best or src.body


def _near_excerpt(trim, keep) -> str:
    runs = shared_runs(trim.tokens, keep.core, min_words=12)
    if runs:
        return _run_text(trim, max(runs, key=lambda r: r[1] - r[0]))
    return _best_shared_paragraph(trim, keep)


def find_near_duplicates(docs) -> list:
    sets = {nid: d.core for nid, d in docs.items() if len(d.tokens) >= MIN_DOC_WORDS}
    out = []
    for (ia, ib), inter in _pair_counts(sets, COMMON_DF).items():
        if inter < NEAR_MIN_SHARED:
            continue
        a, b = docs[ia], docs[ib]
        if _intended(a, b):
            continue
        inter = len(a.core & b.core)
        jac = inter / max(1, len(a.core | b.core))
        contain = inter / max(1, min(len(a.core), len(b.core)))
        if inter < NEAR_MIN_SHARED or (jac < NEAR_JACCARD and contain < NEAR_CONTAIN):
            continue
        keep, trim = sorted((a, b), key=_keeper_key)
        kn, tn = display_name(keep), display_name(trim)
        cut = min(len(trim.tokens), inter + SHINGLE - 1)
        if inter >= 0.9 * max(1, len(trim.core)):
            sug = f"Keep {kn}; {tn} adds almost nothing to it, so cut {tn} to a one-line link to {kn}."
        else:
            sug = f"Keep {kn}; move anything unique from {tn} into it and replace the rest of {tn} with a link."
        out.append({
            "id": _fid("near", keep.id, trim.id), "type": "near_duplicate",
            "notes": [keep.as_ref("keep"), trim.as_ref("trim")],
            "similarity": round(jac, 3), "containment": round(contain, 3),
            "shared_words": inter + SHINGLE - 1, "cut_words": cut,
            "excerpt": excerpt(_near_excerpt(trim, keep)),
            "suggestion": sug,
        })
    return out


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _passage_groups(docs):
    """Union paragraphs (>= PASSAGE_MIN_WORDS) that match exactly or nearly across notes."""
    paras = [(nid, p) for nid in sorted(docs) for p in docs[nid].paras]
    uf, sim = _UF(), {}
    by_key = {}
    for i, (_nid, p) in enumerate(paras):
        by_key.setdefault(p["key"], []).append(i)
    for idx in by_key.values():
        for j in idx[1:]:
            a, b = docs[paras[idx[0]][0]], docs[paras[j][0]]
            if a.id != b.id and not _intended(a, b):
                uf.union(idx[0], j)
                sim[(idx[0], j)] = 1.0
    sets = {i: p["sh"] for i, (_nid, p) in enumerate(paras)}
    for (i, j), c in _pair_counts(sets, COMMON_DF).items():
        if paras[i][0] == paras[j][0]:
            continue
        pi, pj = paras[i][1], paras[j][1]
        small = min(len(pi["sh"]), len(pj["sh"]))
        if c < PASSAGE_CONTAIN * small or _intended(docs[paras[i][0]], docs[paras[j][0]]):
            continue
        inter = len(pi["sh"] & pj["sh"])
        if inter < PASSAGE_CONTAIN * small:
            continue
        uf.union(i, j)
        sim[(i, j)] = max(sim.get((i, j), 0.0), inter / max(1, len(pi["sh"] | pj["sh"])))
    groups = {}
    for i in list(uf.p):
        groups.setdefault(uf.find(i), []).append(i)
    sim_by_group = {}
    for (i, _j), v in sim.items():
        sim_by_group.setdefault(uf.find(i), []).append(v)
    return paras, groups, sim_by_group


def find_passages(docs, near_pairs=()) -> list:
    """Paragraph clusters; ``near_pairs`` are note sets already reported, skipped here."""
    paras, groups, sim_by_group = _passage_groups(docs)
    covered = [frozenset(p) for p in near_pairs]
    template_at = boilerplate_df(len(docs))
    merged = {}       # (keep id, trim ids) -> finding being assembled
    for root in sorted(groups):
        members = groups[root]
        by_doc = {}
        for i in members:
            nid, p = paras[i]
            if nid not in by_doc or p["n"] > by_doc[nid]["n"]:
                by_doc[nid] = p
        ds = [docs[n] for n in by_doc]
        if any(d.kind == "preference" for d in ds):
            ds = [d for d in ds if d.kind not in INTENDED]
        if len(ds) < 2:
            continue
        ids = frozenset(d.id for d in ds)
        if any(ids <= c for c in covered):
            continue  # the pair is already reported (near-duplicate or shared run)
        ds.sort(key=_keeper_key)
        keep, trims = ds[0], ds[1:]
        sims = sim_by_group.get(root) or [1.0]
        key = (keep.id, tuple(d.id for d in trims))
        f = merged.get(key)
        if f is None:
            f = merged[key] = {"keep": keep, "trims": trims, "paras": [], "sim": [], "cut": 0}
        f["paras"].append(by_doc[keep.id])
        f["sim"].append(min(sims))
        f["cut"] += sum(by_doc[d.id]["n"] for d in trims)
    out = []
    for (kid, tids), f in merged.items():
        keep, trims = f["keep"], f["trims"]
        f["paras"].sort(key=lambda p: -p["n"])
        n_par = len(f["paras"])
        words_total = sum(p["n"] for p in f["paras"])
        kn, where = display_name(keep), _names(trims)
        what = "paragraph" if n_par == 1 else f"{n_par} paragraphs"
        copies = len(trims) + 1
        if copies >= template_at:
            sug = (f"The same {what} appears in {copies} notes, so it is probably template text; keep it in "
                   f"{kn} and drop it from the template or link to it from {where}.")
        else:
            sug = f"Keep {kn}; replace the repeated {what} ({words_total} words) in {where} with a link."
        out.append({
            "id": _fid("para", kid, *tids), "type": "passage",
            "notes": [keep.as_ref("keep")] + [d.as_ref("trim") for d in trims],
            "similarity": round(min(f["sim"]), 3), "paragraphs": n_par, "words": words_total,
            "copies": copies, "template": copies >= template_at, "cut_words": f["cut"],
            "excerpt": excerpt(f["paras"][0]["raw"]),
            "suggestion": sug,
        })
    return out


def _run_text(d, run) -> str:
    a, b = run
    start = d.spans[a][0]
    before = d.text[:start].rstrip()
    lead = "…" if before and before[-1] not in ".!?\n|*-#>" else ""
    line_start = d.text.rfind("\n", 0, start) + 1
    if d.text[line_start:start].lstrip().startswith("#"):
        start = line_start  # the run starts on a heading line: drop that line below
        lead = ""
    body = _HEADING_LINE.sub(" ", d.text[start:d.spans[b - 1][1]])
    return lead + body


def find_shared_runs(docs, skip_pairs=()) -> list:
    """Repeated stretches (>= PASSAGE_MIN_WORDS words) that need not align with paragraph
    breaks, e.g. the same bullet list pasted into two role files. Runs are found pairwise,
    then the same stretch seen across several notes becomes one finding."""
    skip = {frozenset(p) for p in skip_pairs}
    sets = {nid: d.core for nid, d in docs.items() if len(d.tokens) >= PASSAGE_MIN_WORDS}
    recs = []    # one per run: keep, trim, words, run, shingles of the run
    for (ia, ib), c in sorted(_pair_counts(sets, COMMON_DF).items()):
        if c < PASSAGE_MIN_WORDS - SHINGLE + 1 or frozenset((ia, ib)) in skip:
            continue
        a, b = docs[ia], docs[ib]
        if _intended(a, b):
            continue
        keep, trim = sorted((a, b), key=_keeper_key)
        for run in shared_runs(trim.tokens, keep.core):
            sh = shingles(trim.tokens[run[0]:run[1]]) & keep.core
            recs.append({"keep": keep, "trim": trim, "run": run, "n": run[1] - run[0], "sh": sh})
    # The same stretch across several notes: union runs that share a note and most shingles.
    uf = _UF()
    by_note = {}
    for i, r in enumerate(recs):
        uf.find(i)
        for d in (r["keep"], r["trim"]):
            by_note.setdefault(d.id, []).append(i)
    for idx in by_note.values():
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                i, j = idx[x], idx[y]
                si, sj = recs[i]["sh"], recs[j]["sh"]
                if len(si & sj) >= 0.6 * max(1, min(len(si), len(sj))):
                    uf.union(i, j)
    clusters = {}
    for i in range(len(recs)):
        clusters.setdefault(uf.find(i), []).append(i)
    pair_findings = {}    # pair -> list of runs (two-note clusters merge per pair)
    multi = []
    for members in clusters.values():
        notes = {}
        for i in members:
            for d in (recs[i]["keep"], recs[i]["trim"]):
                notes[d.id] = d
        if len(notes) == 2:
            key = tuple(sorted(notes))
            pair_findings.setdefault(key, []).extend(recs[i] for i in members)
        else:
            multi.append((notes, [recs[i] for i in members]))
    out = []
    for key, rs in sorted(pair_findings.items()):
        keep, trim = rs[0]["keep"], rs[0]["trim"]
        # Overlapping runs from the same pair are counted once.
        spans_ = sorted({r["run"] for r in rs})
        merged = []
        for s_, e_ in spans_:
            if merged and s_ <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e_))
            else:
                merged.append((s_, e_))
        total = sum(e - s for s, e in merged)
        longest = max(merged, key=lambda r: r[1] - r[0])
        kn, tn = display_name(keep), display_name(trim)
        what = "one passage" if len(merged) == 1 else f"{len(merged)} passages"
        out.append({
            "id": _fid("run", keep.id, trim.id), "type": "passage",
            "notes": [keep.as_ref("keep"), trim.as_ref("trim")],
            "similarity": round(len(keep.core & trim.core) / max(1, len(keep.core | trim.core)), 3),
            "paragraphs": len(merged), "words": total, "copies": 2, "template": False,
            "cut_words": total, "excerpt": excerpt(_run_text(trim, longest)),
            "suggestion": f"Keep {kn}; {tn} repeats {total} words of it in {what}, so replace those with a link to {kn}.",
        })
    for notes, rs in multi:
        ds = sorted(notes.values(), key=_keeper_key)
        keep, trims = ds[0], ds[1:]
        per_trim = {}
        for r in rs:
            for d in (r["keep"], r["trim"]):
                if d.id != keep.id:
                    per_trim[d.id] = max(per_trim.get(d.id, 0), r["n"])
        best = max(rs, key=lambda r: r["n"])
        words_ = best["n"]
        cut = sum(per_trim.values())
        kn, where = display_name(keep), _names(trims)
        same_folder = len({str(Path(d.path).parent) for d in ds}) == 1
        template = len(ds) >= 4 and same_folder
        if template:
            sug = (f"The same passage appears in {len(ds)} notes in one folder, so it is probably template "
                   f"text; keep it in {kn} and drop it from the template or link to it from {where}.")
        else:
            sug = f"Keep {kn}; replace the repeated passage (about {words_} words) in {where} with a link."
        out.append({
            "id": _fid("runs", *sorted(notes)), "type": "passage",
            "notes": [keep.as_ref("keep")] + [d.as_ref("trim") for d in trims],
            "similarity": round(min(len(r["sh"]) / max(1, r["n"] - SHINGLE + 1) for r in rs), 3),
            "paragraphs": 1, "words": words_, "copies": len(ds), "template": template,
            "cut_words": cut, "excerpt": excerpt(_run_text(best["trim"], best["run"])),
            "suggestion": sug,
        })
    return out


def find_superseded(builder, docs) -> list:
    pairs = {}   # (newer id, older id) -> basis
    for nid, node in builder.nodes.items():
        if nid not in docs or node["kind"] not in ("preference", "brain"):
            continue
        pk = "pref" if node["kind"] == "preference" else "brain"
        for ref in node.get("_supersedes", []):
            tgt = builder.resolve_name(ref, pk)
            if tgt in docs and tgt != nid:
                pairs[(nid, tgt)] = "frontmatter"
        for ref in node.get("_superseded_by", []):
            tgt = builder.resolve_name(ref, pk)
            if tgt in docs and tgt != nid:
                pairs[(tgt, nid)] = "frontmatter"
    for (src, dst), kind in builder.links.items():
        if kind == "supersedes" and src in docs and dst in docs and (src, dst) not in pairs \
                and (dst, src) not in pairs:
            pairs[(src, dst)] = "text"
    stems = {}
    for d in docs.values():
        if d.kind not in ("preference", "brain"):
            continue
        for key in {stem_key(Path(d.path).name), stem_key(d.title)}:
            if len(key) >= 4 and key not in GENERIC_STEMS:
                stems.setdefault(key, set()).add(d.id)
    for key, ids in stems.items():
        if len(ids) < 2:
            continue
        ordered = sorted((docs[i] for i in ids), key=lambda d: (d.updated or "", d.mtime), reverse=True)
        newer = ordered[0]
        for older in ordered[1:]:
            if (newer.id, older.id) in pairs or (older.id, newer.id) in pairs:
                continue
            pairs[(newer.id, older.id)] = "same_title"
    out = []
    for (nid, oid), basis in pairs.items():
        new, old = docs[nid], docs[oid]
        if _intended(new, old):
            continue
        if basis != "same_title" and len(old.tokens) < SUPERSEDED_MIN_WORDS:
            continue  # already reduced to a pointer
        inter = len(new.core & old.core)
        jac = inter / max(1, len(new.core | old.core))
        nn, on = display_name(new), display_name(old)
        first = old.paras[0]["raw"] if old.paras else (paragraphs(old.body) or [old.body])[0]
        cut, quote = len(old.tokens), first
        if basis == "same_title":
            sug = (f"{nn} and {on} share a title; if {nn} is the current version, mark {on} "
                   f"superseded_by it and cut {on} to a one-line pointer.")
        elif basis == "frontmatter":
            sug = (f"{nn} supersedes {on} (frontmatter) but {on} still holds {len(old.tokens)} words; "
                   f"cut {on} to a one-line pointer to {nn}.")
        else:
            # A sentence says part of the older note is superseded: point at that paragraph only.
            for p in paragraphs(old.body):
                if re.search(r"supersed", p, re.I):
                    cut, quote = len(words(p)), p
                    break
            sug = (f"{nn} is noted as superseding part of {on}; remove the superseded wording from {on} "
                   f"and point to {nn} instead.")
        out.append({
            "id": _fid("sup", nid, oid), "type": "superseded", "basis": basis,
            "notes": [new.as_ref("keep"), old.as_ref("trim")],
            "similarity": round(jac, 3), "cut_words": cut,
            "excerpt": excerpt(quote),
            "suggestion": sug,
        })
    return out


# ---------------------------------------------------------------- build
def analyse(drive_root_path=None, disk_root_path=None) -> dict:
    drive = Path(drive_root_path) if drive_root_path else bi.drive_root()
    disk = Path(disk_root_path) if disk_root_path else bi.disk_root()
    b = _NotesOnly(drive, disk)
    b.collect()
    b.build_links()
    docs = _docs(b)
    near = find_near_duplicates(docs)
    near_pairs = [tuple(n["id"] for n in f["notes"]) for f in near]
    runs = find_shared_runs(docs, near_pairs)
    covered = near_pairs + [tuple(n["id"] for n in f["notes"]) for f in runs]
    passages = runs + find_passages(docs, covered)
    sup = find_superseded(b, docs)
    findings = near + passages + sup
    order = {"near_duplicate": 0, "superseded": 1, "passage": 2}
    findings.sort(key=lambda f: (-f["cut_words"], order[f["type"]], f["id"]))
    counts = {"near_duplicate": len(near), "passage": len(passages), "superseded": len(sup),
              "total": len(findings)}
    return {
        "schema_version": SCHEMA_VERSION, "algo_version": ALGO_VERSION, "generated": True,
        "read_only": True,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": {"drive": str(drive), "disk": str(disk)},
        "notes_scanned": len(docs),
        "counts": counts,
        "cut_words_total": sum(f["cut_words"] for f in findings),
        "thresholds": {"shingle": SHINGLE, "near_jaccard": NEAR_JACCARD, "near_containment": NEAR_CONTAIN,
                       "near_min_shared": NEAR_MIN_SHARED, "passage_min_words": PASSAGE_MIN_WORDS,
                       "passage_containment": PASSAGE_CONTAIN},
        "findings": findings,
    }


def load_cached(cache_path=None):
    cache = Path(cache_path) if cache_path else default_cache_path()
    try:
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION \
            or data.get("algo_version") != ALGO_VERSION:
        return None
    return data


def get_overlaps(refresh=False, cache_path=None, drive_root_path=None, disk_root_path=None,
                 write_cache=True) -> dict:
    """Return the cached analysis while every source mtime is unchanged, else rebuild it."""
    drive = Path(drive_root_path) if drive_root_path else bi.drive_root()
    disk = Path(disk_root_path) if disk_root_path else bi.disk_root()
    cache = Path(cache_path) if cache_path else default_cache_path()
    with _LOCK:
        mtimes = source_mtimes(drive)
        if not refresh:
            cached = load_cached(cache)
            if cached and cached.get("roots") == {"drive": str(drive), "disk": str(disk)} \
                    and cached.get("sources") == mtimes:
                cached["cached"] = True
                return cached
        result = analyse(drive, disk)
        result["sources"] = mtimes
        if write_cache:
            bi._guard_cache_path(cache, drive, disk)
            bi._write_atomic(cache, result)
        result["cached"] = False
        return result


def r_overlaps(h, q):
    """GET /api/brain/overlaps[?refresh=1] -> findings (source mtimes left out of the reply)."""
    data = get_overlaps(refresh=str(q.get("refresh", "")).lower() in ("1", "true", "yes"))
    out = {k: v for k, v in data.items() if k != "sources"}
    out["source_count"] = len(data.get("sources") or {})
    return out


ROUTES = {ROUTE: r_overlaps}


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Find duplicate material in the brain (read-only).")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--show", type=int, default=0, help="print the top N findings")
    a = ap.parse_args(argv)
    r = get_overlaps(refresh=a.refresh or a.no_cache, write_cache=not a.no_cache)
    print(json.dumps({"notes_scanned": r["notes_scanned"], "counts": r["counts"],
                      "cut_words_total": r["cut_words_total"], "cached": r.get("cached")}, indent=1))
    for f in r["findings"][: a.show]:
        print(f"- [{f['type']}] cut~{f['cut_words']} sim={f['similarity']}: {f['suggestion']}")
        print("    " + f["excerpt"][:160])


if __name__ == "__main__":
    _main()
