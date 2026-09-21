"""Registry names must not depend on the order scan-add was run in (eca-rsi#11).

The old rule gave the bare `/Bladder/` URL to whichever collection was scanned
first and qualified everyone else, so rebuilding the registry in a different
order silently moved a bookmarked link to another batch's data.
"""
from pathlib import Path

from ecarsi.serve import _scan_names


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
