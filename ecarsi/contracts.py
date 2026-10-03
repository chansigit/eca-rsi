"""The JSON files two subsystems share (decision 0004): the fields a writer must put in and a reader
may rely on -- only those; a field nobody else reads stays out, so older files keep passing. The writer checks before it writes and the reader after it reads, so a change on one side
fails at the other's door with the field named, not stages later as a KeyError.

A file names its version in `schema`; a file without one is version 1, as written before this module
existed. Publications are immutable and resumed runs rewrite them byte for byte, so they stay unstamped
version 1 until a version 2 is needed. To change a file's fields, add the next version here, make the
readers accept both, then make the writer stamp it.
"""
REF = dict  # {"path": ..., "sha256": ...}
COUNTS = {"n_input": int, "n_survived": int, "n_removed": int}
STATE = ("complete", "incomplete")

KINDS = {
    # <display zone>/display.json: written by ecarsi.display.sync, read by Periscope's display-root scan
    "display/1": {"name": str, "collection": str, "dataset": str, "run": str, "source": str, "work": str},
    # <unit>/01-per-sample/publication.json: control/persample, read by cross-sample and release
    "per-sample/1": {"state": STATE, "samples": list, "failed_samples": list, "partition_exclusions": REF, **COUNTS},
    # <unit>/rounds/roundNN/{02-cross-sample,03-zoom-in}/publication.json: control/crosssample, control/zoomin
    "stage/1": {"state": STATE, "input": REF, "files": dict, "result": REF, **COUNTS},
    # <unit>/rounds/roundNN/publication.json: the round's decision (control/dataset), read by release
    "round/1": {"round": int, "stats": dict, "cross_sample": REF, "zoom_in": REF},
    # <unit>/publication.json: the released unit (control/dataset), read by release and the dataset step
    "unit/1": {"state": STATE, "unit": dict, "per_sample": REF, "rounds": list, "final": REF,
               "forced_release": bool, "reason": str, **COUNTS},
    # <run>/publication.json: the dataset (control/dataset)
    "dataset/1": {"state": STATE, "dataset_id": str, "units": list, "failed_units": list, "forced_release": bool, **COUNTS},
    # <run>/degraded/*.json: ecarsi.degraded, read by release and Periscope
    "degraded/1": {"what": str, "error": str, "at": int, "id": str},
}


def check(kind: str, record) -> dict:
    """`record` if it meets the version of `kind` it names (none: 1); otherwise ValueError naming every problem."""
    if not isinstance(record, dict):
        raise ValueError(f"{kind}: not a JSON object")
    version = record.get("schema", f"{kind}/1")
    if version.split("/")[0] != kind or version not in KINDS:
        raise ValueError(f"{kind}: unknown schema {version!r}")
    problems = []
    for field, expected in KINDS[version].items():
        value = record.get(field)
        if field not in record:
            problems.append(f"{field} missing")
        elif isinstance(expected, tuple) and value not in expected:
            problems.append(f"{field} is {value!r}, not one of {expected}")
        elif not isinstance(expected, tuple) and not isinstance(value, expected):
            problems.append(f"{field} is {type(value).__name__}, not {expected.__name__}")
    if problems:
        raise ValueError(f"{version}: " + "; ".join(problems))
    return record
