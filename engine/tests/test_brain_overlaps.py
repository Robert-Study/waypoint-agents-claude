"""brain_overlaps: near-duplicates, repeated passages, superseded pairs (tmp fixture trees)."""
import hashlib
import json
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_engine import brain_overlaps as bo  # noqa: E402

GOV = "06 System & Governance"
VOCAB = ["w%03d" % i for i in range(900)]


def prose(seed, n):
    """Deterministic filler with no 5-word overlap between different seeds (in practice)."""
    r = random.Random(seed)
    return " ".join(r.choice(VOCAB) for _ in range(n)) + "."


def _w(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def note(ident, title, status, body, **extra):
    fm = {"schema_version": 1, "id": ident, "title": title, "evidence_status": status,
          "source_ids": ["s1"], "scope": "global", "tags": ["preferences", "ui"]}
    fm.update(extra)
    return "---\n" + json.dumps(fm, indent=2) + "\n---\n\n# " + title + "\n\n" + body + "\n"


SHARED = prose("shared-passage", 60)      # one paragraph repeated in two brain notes
PREF_RULE = prose("pref-rule", 80)        # a preference restated by a skill (intended)
OLD_BODY = prose("old-body", 70)


@pytest.fixture()
def tree(tmp_path):
    drive = tmp_path / "drive"
    disk = tmp_path / "disk"
    disk.mkdir()
    ex = drive / "notes"
    prefs = ex / GOV / "Preferences"
    brain = ex / GOV / "Brain"
    base = prose("ui-core", 220)
    _w(prefs / "01_ui_core.md", note("01_ui_core", "Dense visual tools", "explicit_user_statement",
                                      "## Core Idea\n\n" + base + "\n\n" + PREF_RULE))
    # Near-duplicate: nearly the same body, a few words changed, weaker provenance.
    toks = base.split()
    toks[50] = "changed"
    toks[120] = "edited"
    _w(prefs / "02_ui_copy.md", note("02_ui_copy", "Dense tools again", "agent_synthesis",
                                      "## Core Idea\n\n" + " ".join(toks) + "\n\n" + prose("copy-extra", 20)))
    _w(prefs / "03_old_rule.md", note("03_old_rule", "Old rule", "explicit_user_statement",
                                      "## Core Idea\n\n" + OLD_BODY))
    _w(prefs / "04_new_rule.md", note("04_new_rule", "New rule", "explicit_user_statement",
                                      "## Core Idea\n\n" + prose("new-body", 70), supersedes=["03_old_rule"]))
    _w(prefs / "05_unrelated.md", note("05_unrelated", "Unrelated", "explicit_user_statement",
                                        "## Core Idea\n\n" + prose("unrelated", 200)))
    # Generated compilation: never scanned.
    _w(prefs / "USER_PREFERENCES_AND_PHILOSOPHY.md", base + "\n\n" + SHARED)
    _w(brain / "Doctrine" / "doctrine-a.md",
       "---\nid: doctrine-a\ntitle: Doctrine A\nevidence_status: vetted_source\n---\n\n# Doctrine A\n\n"
       + prose("doc-a", 50) + "\n\n" + SHARED + "\n\n## Provenance\n\n" + prose("boiler", 45) + "\n")
    _w(brain / "Mental Models" / "model-b.md",
       "---\nid: model-b\ntitle: Model B\nevidence_status: agent_synthesis\n---\n\n# Model B\n\n"
       + prose("mod-b", 50) + "\n\n" + SHARED + "\n\n## Provenance\n\n" + prose("boiler", 45) + "\n")
    _w(brain / "Project Learnings" / "release_checklist.md",
       "---\nid: release_checklist\ntitle: Release checklist\n---\n\n# Release checklist\n\n" + prose("rel1", 60) + "\n")
    _w(brain / "Project Learnings" / "release_checklist_v2.md",
       "---\nid: release_checklist_v2\ntitle: Release checklist v2\nupdated_at: 2026-11-01\n---\n\n"
       "# Release checklist v2\n\n" + prose("rel2", 60) + "\n")
    _w(brain / "INDEX.md", "# Brain index\n")
    # A skill that restates the preference rule word for word: intended, never reported.
    _w(ex / "07 Skills Registry" / "ui-skill" / "SKILL.md",
       "---\nname: ui-skill\ndescription: UI rules\n---\n\n# UI skill\n\n" + PREF_RULE + "\n")
    _w(ex / "AGENTS.md", "# Agents\n\n" + prose("agents", 80) + "\n")
    return drive, disk, tmp_path / "data" / "brain_overlaps.json"


def run(tree, **kw):
    drive, disk, cache = tree
    return bo.get_overlaps(drive_root_path=drive, disk_root_path=disk, cache_path=cache, **kw)


def by_type(res, t):
    return [f for f in res["findings"] if f["type"] == t]


def ids(f):
    return [n["id"] for n in f["notes"]]


def digest(root: Path):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def test_near_duplicate_keeps_stronger_provenance(tree):
    res = run(tree)
    near = by_type(res, "near_duplicate")
    assert len(near) == 1
    f = near[0]
    assert ids(f) == ["pref:01_ui_core", "pref:02_ui_copy"]
    assert [n["role"] for n in f["notes"]] == ["keep", "trim"]
    assert f["similarity"] >= bo.NEAR_JACCARD or f["containment"] >= bo.NEAR_CONTAIN
    assert f["suggestion"].startswith("Keep 01_ui_core.md;") and "02_ui_copy.md" in f["suggestion"]
    assert f["suggestion"].count(".") >= 1 and "\n" not in f["suggestion"]
    assert 0 < len(f["excerpt"]) <= bo.EXCERPT_CHARS
    assert f["cut_words"] > 150
    assert not f["excerpt"].lstrip("…").startswith("Core Idea")    # headings never quoted


def test_repeated_paragraph_across_brain_notes(tree):
    res = run(tree)
    passages = by_type(res, "passage")
    pair = [f for f in passages if set(ids(f)) == {"brain:doctrine-a", "brain:model-b"}]
    assert len(pair) == 1
    f = pair[0]
    assert ids(f)[0] == "brain:doctrine-a"        # vetted source beats agent synthesis
    assert f["words"] >= bo.PASSAGE_MIN_WORDS and f["cut_words"] >= bo.PASSAGE_MIN_WORDS
    assert f["excerpt"].strip("…").split()[0] in SHARED
    assert len(f["excerpt"]) <= bo.EXCERPT_CHARS
    assert "model-b.md" in f["suggestion"] and f["suggestion"].startswith("Keep doctrine-a.md")


def test_preference_restated_by_skill_is_not_reported(tree):
    res = run(tree)
    for f in res["findings"]:
        kinds = {n["kind"] for n in f["notes"]}
        assert not ("preference" in kinds and kinds & {"skill", "instruction"}), f
    assert not any("skill:ui-skill" in ids(f) for f in res["findings"])


def test_metadata_sections_and_compilations_ignored(tree):
    res = run(tree)
    for f in res["findings"]:
        assert all("USER_PREFERENCES" not in n["path"] for n in f["notes"])
    # The shared Provenance boilerplate alone must not make a finding.
    assert bo.content("intro\n\n## Provenance\n\nboiler\n\n### Sub\n\nmore\n\n## Next\n\nkept") == \
        "intro\n\n## Next\n\nkept"


def test_superseded_frontmatter_and_same_title(tree):
    res = run(tree)
    sup = by_type(res, "superseded")
    fm = [f for f in sup if f["basis"] == "frontmatter"]
    assert len(fm) == 1 and ids(fm[0]) == ["pref:04_new_rule", "pref:03_old_rule"]
    assert "one-line pointer" in fm[0]["suggestion"]
    same = [f for f in sup if f["basis"] == "same_title"]
    assert len(same) == 1
    assert ids(same[0]) == ["brain:release_checklist_v2", "brain:release_checklist"]


def test_unrelated_notes_produce_nothing(tree):
    res = run(tree)
    for f in res["findings"]:
        assert "pref:05_unrelated" not in ids(f)
        assert "instr:AGENTS.md" not in ids(f)
    assert res["counts"]["total"] == len(res["findings"]) == 4


def test_sorted_by_cut_words(tree):
    res = run(tree)
    cuts = [f["cut_words"] for f in res["findings"]]
    assert cuts == sorted(cuts, reverse=True)
    assert len({f["id"] for f in res["findings"]}) == len(res["findings"])


def test_read_only_and_cache_with_mtimes(tree):
    drive, disk, cache = tree
    before = digest(drive)
    first = run(tree)
    assert digest(drive) == before                       # sources untouched
    assert cache.is_file() and first["cached"] is False
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["generated"] is True and stored["sources"]
    assert all(isinstance(v, int) for v in stored["sources"].values())
    second = run(tree)
    assert second["cached"] is True and second["findings"] == first["findings"]
    # Touch a source: the cache no longer matches and is rebuilt.
    p = drive / "notes" / GOV / "Preferences" / "05_unrelated.md"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    third = run(tree)
    assert third["cached"] is False
    assert run(tree, refresh=True)["cached"] is False


def test_cache_inside_sources_refused(tree):
    drive, disk, _ = tree
    with pytest.raises(ValueError):
        bo.get_overlaps(drive_root_path=drive, disk_root_path=disk,
                        cache_path=drive / "notes" / "overlaps.json")


def test_stem_key():
    assert bo.stem_key("06_ui_and_anti_slop.md") == "ui and anti slop"
    assert bo.stem_key("release_checklist_v2.md") == bo.stem_key("release_checklist.md")
    assert bo.stem_key("reel-32.md") != bo.stem_key("reel-03.md")


def test_shared_runs_bridge_one_word_edit():
    a = prose("run-a", 80).rstrip(".").split()
    b = list(a)
    b[40] = "different"
    runs = bo.shared_runs(b, bo.shingles(a))
    assert runs == [(0, 80)]
    assert bo.shared_runs(prose("other", 80).split(), bo.shingles(a)) == []


def test_route_handler_with_fake_h(tree, monkeypatch):
    drive, disk, cache = tree
    monkeypatch.setenv("AI_DRIVE_ROOT", str(drive))
    monkeypatch.setenv("AI_DISK_ROOT", str(disk))
    monkeypatch.setenv("CC_DATA_DIR", str(cache.parent))

    class FakeH:
        app = None

    assert list(bo.ROUTES) == ["GET /api/brain/overlaps"]
    handler = bo.ROUTES["GET /api/brain/overlaps"]
    out = handler(FakeH(), {})
    assert "sources" not in out and out["source_count"] > 0
    assert out["counts"]["total"] == len(out["findings"]) and out["read_only"] is True
    assert (cache.parent / "brain_overlaps.json").is_file()
    assert handler(FakeH(), {})["cached"] is True
    assert handler(FakeH(), {"refresh": "1"})["cached"] is False
    json.dumps(out)    # JSON-serialisable
