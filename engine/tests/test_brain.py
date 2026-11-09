import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_engine import brain_index as bi  # noqa: E402

GOV = "06 System & Governance"


def _w(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json_note(ident, title, status, tags, related=(), sources=("s1",), body=""):
    fm = {"schema_version": 1, "id": ident, "title": title, "evidence_status": status,
          "source_ids": list(sources), "scope": "global", "tags": ["preferences", *tags],
          "related": list(related)}
    return "---\n" + json.dumps(fm, indent=2) + "\n---\n\n# " + title + "\n\n## Core Idea\n\n" + \
        (body or f"{title} core idea.") + "\n"


@pytest.fixture()
def tree(tmp_path):
    drive = tmp_path / "drive"
    disk = tmp_path / "disk"
    ex = drive / "notes"
    prefs = ex / GOV / "Preferences"
    brain = ex / GOV / "Brain"
    _w(prefs / "01_ui_note.md", _json_note("01_ui_note", "Dense visual tools", "explicit_user_statement",
                                           ["ui", "graph"], sources=("a", "b")))
    _w(prefs / "02_budget_note.md",
       "---\nid: 02_budget_note\ntitle: Budget carefully\nevidence_status: user_reported_calibration\n"
       "source_ids: [c]\ntags:\n  - preferences\n  - budget\n  - models\nrelated: [01_ui_note]\n---\n\n"
       "# Budget carefully\n\n## Core Idea\n\nSee [[03_old]] and [missing](99_nope.md).\n")
    _w(prefs / "03_old.md", _json_note("03_old", "Old filing rule", "inherited_registry", ["filing"]))
    _w(prefs / "04_new.md", _json_note("04_new", "New filing rule", "explicit_user_statement",
                                       ["filing", "drive"], body="This supersedes the wording in note 03."))
    _w(prefs / "INDEX.md", "# Index\n\n- [a](01_ui_note.md)\n- [b](02_budget_note.md)\n")
    _w(prefs / "USER_PREFERENCES_AND_PHILOSOPHY.md", "# compiled copy\n")
    _w(prefs / "Sources" / "a.json", "{}")
    _w(brain / "INDEX.md", "# Brain\n\nAsk the `alpha` skill.\n")
    _w(brain / "CONTRACT.md", "# Contract\n\nPreferences keep `../Preferences/INDEX.md`. See Preferences/INDEX.md.\n")
    _w(brain / "Doctrine" / "doctrine-a.md",
       '---\n{"id": "doctrine-a", "title": "Doctrine A", "kind": "doctrine", "provenance": "synthesised",'
       ' "evidence_status": "agent_synthesis", "source_ids": ["x"], "tags": ["doctrine"],'
       ' "related": ["pref:01_ui_note"]}\n---\n\n# Doctrine A\n')
    _w(brain / "My Words" / "mw-1.md",
       '---\n{"id": "mw-1", "title": "my words", "evidence_status": "explicit_user_statement",'
       ' "source_ids": ["m"], "tags": ["my-words"], "related": []}\n---\n\n# my words\n\n'
       "Link: [budget](../../Preferences/02_budget_note.md)\n")
    _w(brain / "Evidence" / "x.json", "{}")
    _w(ex / GOV / "Agent Roles" / "ORCHESTRATOR.md", "# Orchestrator\n\nUse 07 Skills Registry\\beta\\SKILL.md.\n")
    _w(ex / "AGENTS.md", "# Agents\n\nRead `06 System & Governance\\Agent Roles\\ORCHESTRATOR.md` and "
                         "Preferences\\INDEX.md. Apply `07 Skills Registry\\alpha\\SKILL.md`.\n")
    _w(ex / "07 Skills Registry" / "alpha" / "SKILL.md",
       "---\nname: alpha\ndescription: Alpha skill.\n---\n\n# Alpha\n\nFollow 01_ui_note always.\n")
    _w(ex / "07 Skills Registry" / "beta" / "SKILL.md", "---\nname: beta\ndescription: Beta.\n---\n# Beta\n")
    (ex / "07 Skills Registry" / "not_a_skill").mkdir(parents=True)
    (ex / "05 Subagent Pipeline" / "Outputs" / "deep3").mkdir(parents=True)
    (ex / ".git").mkdir()
    for d in ("repoA/src/inner", "repoA/data/x", "repoA/node_modules/y", "repoA/.venv", "repoB/.git", "data"):
        (disk / d).mkdir(parents=True, exist_ok=True)
    return drive, disk


def _snapshot(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.stat()
            out[str(p.relative_to(root))] = (p.is_dir(), st.st_size if p.is_file() else 0, st.st_mtime_ns)
    return out


def _build(tree, tmp_path, **kw):
    drive, disk = tree
    cache = tmp_path / "cc_data" / "brain_index.json"
    return bi.build_graph(drive, disk, cache_path=cache, **kw), cache


def test_brain_counts_per_kind(tree, tmp_path):
    g, _ = _build(tree, tmp_path)
    c = g["counts"]
    assert c["preference"] == 4          # INDEX and the compiled copy are not preferences
    assert c["brain"] == 2
    assert c["skill"] == 2               # folder without SKILL.md is skipped
    assert c["instruction"] == 5         # AGENTS, ORCHESTRATOR, two INDEX files, Brain CONTRACT
    assert set(c) == set(bi.KINDS)
    kinds = {n["kind"] for n in g["nodes"]}
    assert kinds == set(bi.KINDS)
    for n in g["nodes"]:
        assert set(("id", "label", "kind", "cluster", "path", "provenance", "weight")) <= set(n)


def test_brain_folder_tree_is_shallow_and_skips_noise(tree, tmp_path):
    g, _ = _build(tree, tmp_path)
    folders = [n for n in g["nodes"] if n["kind"] == "folder"]
    labels = {n["label"] for n in folders}
    assert {"notes", "disk", "repoA", "repoB", "src", "Outputs", "Preferences"} <= labels
    assert not labels & {".git", ".venv", "node_modules", "data", "inner", "deep3"}
    assert all(n["depth"] <= 2 for n in folders if "depth" in n)


def test_brain_provenance_brightness_and_weight(tree, tmp_path):
    g, _ = _build(tree, tmp_path)
    by = {n["id"]: n for n in g["nodes"]}
    ui, budget, old = by["pref:01_ui_note"], by["pref:02_budget_note"], by["pref:03_old"]
    assert ui["provenance"] == "explicit_user_statement"
    assert budget["provenance"] == "user_reported_calibration"
    assert old["provenance"] == "inherited_registry"
    assert ui["brightness"] > budget["brightness"] > old["brightness"]
    assert ui["weight"] == 2 and budget["weight"] == 1
    assert by["brain:doctrine-a"]["provenance"] == "agent_synthesis"
    assert by["brain:doctrine-a"]["cluster"] == "Doctrine"


def test_brain_preference_clusters_by_tag_family(tree, tmp_path):
    g, _ = _build(tree, tmp_path)
    by = {n["id"]: n for n in g["nodes"]}
    assert by["pref:01_ui_note"]["cluster"] == "UI & design"
    assert by["pref:02_budget_note"]["cluster"] == "Models & budget"
    assert by["pref:03_old"]["cluster"] == "Knowledge & filing"
    assert set(g["preference_clusters"]) <= set(bi.CLUSTERS)


def test_brain_links_resolve_and_have_expected_kinds(tree, tmp_path):
    g, _ = _build(tree, tmp_path)
    ids = {n["id"] for n in g["nodes"]}
    links = {(l["source"], l["target"]): l["kind"] for l in g["links"]}
    for l in g["links"]:
        assert l["source"] in ids and l["target"] in ids
        assert l["kind"] in bi.LINK_KINDS
    assert links[("pref:02_budget_note", "pref:01_ui_note")] == "related"
    assert links[("pref:02_budget_note", "pref:03_old")] == "link"            # [[wikilink]]
    assert links[("pref:04_new", "pref:03_old")] == "supersedes"
    assert links[("brain:doctrine-a", "pref:01_ui_note")] == "related"        # pref: prefix
    assert links[("brain:mw-1", "pref:02_budget_note")] == "link"             # relative markdown link
    assert links[("instr:06 System & Governance/Preferences/INDEX.md", "pref:01_ui_note")] == "link"
    assert links[("folder:notes/06 System & Governance/Preferences", "pref:01_ui_note")] == "contains"
    assert links[("folder:notes/07 Skills Registry/alpha", "skill:alpha")] == "contains"
    assert links[("folder:notes", "instr:AGENTS.md")] == "contains"
    assert links[("folder:notes", "folder:notes/06 System & Governance")] == "contains"
    assert links[("instr:AGENTS.md", "instr:06 System & Governance/Agent Roles/ORCHESTRATOR.md")] == "cites"
    assert links[("instr:AGENTS.md", "skill:alpha")] == "cites"
    assert links[("instr:AGENTS.md", "instr:06 System & Governance/Preferences/INDEX.md")] == "cites"
    assert links[("instr:06 System & Governance/Agent Roles/ORCHESTRATOR.md", "skill:beta")] == "cites"
    assert links[("skill:alpha", "pref:01_ui_note")] == "cites"
    assert g["unresolved_links"] >= 1                                         # 99_nope.md
    assert g["link_counts"]["contains"] > 0


def test_brain_writes_only_the_cache(tree, tmp_path):
    drive, disk = tree
    before_d, before_k = _snapshot(drive), _snapshot(disk)
    g, cache = _build(tree, tmp_path)
    assert _snapshot(drive) == before_d
    assert _snapshot(disk) == before_k
    assert sorted(p.name for p in cache.parent.iterdir()) == ["brain_index.json"]
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["generated"] is True and data["counts"] == g["counts"]


def test_brain_refuses_cache_inside_sources(tree, tmp_path):
    drive, disk = tree
    bad = drive / "notes" / "brain_index.json"
    with pytest.raises(ValueError):
        bi.build_graph(drive, disk, cache_path=bad)
    assert not bad.exists()


def test_brain_env_root_and_cache_reuse(tree, tmp_path, monkeypatch):
    drive, disk = tree
    monkeypatch.setenv("AI_DRIVE_ROOT", str(drive))
    monkeypatch.setenv("AI_DISK_ROOT", str(disk))
    cache = tmp_path / "d2" / "brain_index.json"
    first = bi.get_graph(cache_path=cache)
    assert first["roots"]["drive"] == str(drive) and first["counts"]["preference"] == 4
    again = bi.get_graph(cache_path=cache)
    assert again.get("cached") is True
    fresh = bi.get_graph(refresh=True, cache_path=cache)
    assert not fresh.get("cached")


def test_brain_frontmatter_parsers():
    fm, body = bi.parse_frontmatter('---\n{"id": "x", "tags": ["a"]}\n---\nbody')
    assert fm == {"id": "x", "tags": ["a"]} and body == "body"
    fm, _ = bi.parse_frontmatter("---\nid: y\ntags: [a, 'b']\nrelated:\n  - z\n---\n")
    assert fm == {"id": "y", "tags": ["a", "b"], "related": ["z"]}
    assert bi.parse_frontmatter("no frontmatter")[0] == {}


REAL_DRIVE = Path(bi.DEFAULT_DRIVE_ROOT) / "notes" / GOV / "Preferences"


@pytest.mark.skipif(not REAL_DRIVE.is_dir(), reason="real Drive not present")
def test_brain_real_drive_has_at_least_80_preferences():
    g = bi.build_graph(bi.DEFAULT_DRIVE_ROOT, bi.DEFAULT_DISK_ROOT, write_cache=False)
    assert g["counts"]["preference"] >= 80
    assert g["counts"]["skill"] > 0 and g["counts"]["instruction"] > 0 and g["counts"]["folder"] > 0
    ids = {n["id"] for n in g["nodes"]}
    assert all(l["source"] in ids and l["target"] in ids for l in g["links"])
