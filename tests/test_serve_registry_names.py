"""Registry names must not depend on the order scan-add was run in (eca-rsi#11).

The old rule gave the bare `/Bladder/` URL to whichever collection was scanned
first and qualified everyone else, so rebuilding the registry in a different
order silently moved a bookmarked link to another batch's data.
"""
from pathlib import Path

from ecarsi.ui.registry import _scan_names


def paths(*specs: str) -> list[Path]:
    return [Path("/data") / s / "eca-pp" / "Bladder" / "rsi" for s in specs]


def test_bare_name_is_requalified_when_a_second_batch_arrives():
    first, second = paths("mca1.1"), paths("mca3.0")
    names = _scan_names(second, {"Bladder": first[0]})
    assert names[second[0]] == "mca3.0-Bladder"
    assert names[first[0]] == "mca1.1-Bladder", "the incumbent keeps the bare name"


def test_result_is_the_same_whichever_batch_was_scanned_first():
    a, b, c = paths("mca1.1")[0], paths("mca3.0")[0], paths("tabula-muris-facs")[0]
    one_shot = _scan_names([a, b, c], {})
    incremental: dict[str, Path] = {}
    for d in (a, b, c):  # three separate scan-add invocations
        resolved = _scan_names([d], dict(incremental))
        incremental = {n: p for p, n in resolved.items()}
    assert {p: n for p, n in one_shot.items()} == incremental_by_path(incremental)
    assert set(one_shot.values()) == {"mca1.1-Bladder", "mca3.0-Bladder", "tabula-muris-facs-Bladder"}


def incremental_by_path(by_name: dict[str, Path]) -> dict[Path, str]:
    return {p: n for n, p in by_name.items()}


def test_a_deliberate_name_is_never_renamed():
    first, second = paths("mca1.1"), paths("mca3.0")
    names = _scan_names(second, {"my-favourite-bladder": first[0]})
    assert first[0] not in names, "a custom --name is not up for renaming"
    assert names[second[0]] == "Bladder", "and it does not block the bare name either"


def test_a_lone_dataset_still_gets_the_short_name():
    only = paths("mca1.1")
    assert _scan_names(only, {}) == {only[0]: "Bladder"}


def test_an_existing_name_is_never_shortened():
    """Requalification is one-way. Recomputing depth from scratch would have renamed 298 of the
    459 live entries, every one to something shorter -- churn and dead links for no collision."""
    qualified = paths("3ca")[0]
    names = _scan_names([], {"3ca-Bladder": qualified})
    assert names[qualified] == "3ca-Bladder"


def test_a_settled_registry_proposes_no_renames():
    a, b = paths("mca1.1")[0], paths("mca3.0")[0]
    settled = {"mca1.1-Bladder": a, "mca3.0-Bladder": b}
    assert _scan_names([], settled) == {a: "mca1.1-Bladder", b: "mca3.0-Bladder"}


def test_display_roots_in_the_config_serve_every_display_zone_under_its_recorded_name(tmp_path):
    """ops/display-zone.py copies a run's pages to <root>/<collection>/<dataset>/<run>/ with a display.json;
    naming the root in results.json serves them all, under the file and the command line."""
    import json
    from ecarsi.ui.serve import Registry
    root = tmp_path / "display"
    def record(name, coll, ds, run):  # what ecarsi.display.sync writes (ecarsi.contracts display/1)
        return dict(name=name, collection=coll, dataset=ds, run=run, source=f"/runs/{name}", work=f"/work/{run}.tar.gz")
    for coll, ds, run, name in (("3ca", "Durante", "gen2-20260917", "3ca-Durante"), ("hcl", "Adipose", "gen1", "hcl-Adipose"),
                                ("hcl", "Adipose", "gen1-copy", "hcl-Adipose")):
        (root / coll / ds / run).mkdir(parents=True)
        (root / coll / ds / run / "display.json").write_text(json.dumps(record(name, coll, ds, run)))
    (root / "hcl" / "Broken" / "gen1").mkdir(parents=True)
    (root / "hcl" / "Broken" / "gen1" / "display.json").write_text("{")
    config = tmp_path / "results.json"
    config.write_text(json.dumps({"display_root": str(root), "archive_root": "/a", "more_display_roots": [str(tmp_path / "gone")]}))
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps({"3ca-Durante": str(tmp_path / "elsewhere"), "kept": str(root / "hcl" / "Adipose" / "gen1")}))
    reg = Registry(registry_file, config=config)
    assert reg.snapshot() == {"3ca-Durante": tmp_path / "elsewhere", "kept": root / "hcl" / "Adipose" / "gen1"}  # not scanned yet
    reg.scan_display()
    # the file wins by name and by path; a second claim on a name gets its run appended; a broken record is skipped
    assert reg.snapshot() == {"3ca-Durante": tmp_path / "elsewhere", "kept": root / "hcl" / "Adipose" / "gen1",
                              "hcl-Adipose-gen1-copy": root / "hcl" / "Adipose" / "gen1-copy"}
    registry_file.write_text("{}")
    assert reg.snapshot() == {"3ca-Durante": root / "3ca" / "Durante" / "gen2-20260917", "hcl-Adipose": root / "hcl" / "Adipose" / "gen1",
                              "hcl-Adipose-gen1-copy": root / "hcl" / "Adipose" / "gen1-copy"}
    # a dataset run again: the newest copy takes the name
    (root / "hcl" / "Adipose" / "gen1-copy" / "display.json").write_text(
        json.dumps(dict(record("hcl-Adipose", "hcl", "Adipose", "gen1-copy"), synced_at="2026-10-05")))
    reg.scan_display()
    assert reg.snapshot()["hcl-Adipose"] == root / "hcl" / "Adipose" / "gen1-copy"
    assert Registry(registry_file, config=tmp_path / "missing.json").snapshot() == {}
