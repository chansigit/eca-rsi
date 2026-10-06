"""ecarsi.contracts: a file two subsystems share fails at the door with its fields named."""
import pytest

from ecarsi.contracts import check

REF = {"path": "/x/publication.json", "sha256": "0" * 64}
DISPLAY = dict(name="pbmc68k", collection="pbmc68k", dataset="pbmc68k_zheng2017", run="gen1", source="/src", work="/w.tar.zst")


def test_a_file_without_schema_is_version_one_and_extra_fields_are_free():
    assert check("display", {**DISPLAY, "copied_at": "2026-10-02"})["name"] == "pbmc68k"
    assert check("display", {**DISPLAY, "schema": "display/1"})
    assert check("stage", dict(state="complete", input=REF, files={}, result=REF, n_input=3, n_survived=2, n_removed=1,
                               lineages=[], planning=REF))


def test_problems_are_named_together():
    with pytest.raises(ValueError) as caught:
        check("stage", dict(state="done", input=REF, files={}, result="x", n_input=3, n_survived=2))
    message = str(caught.value)
    assert "stage/1" in message and "state is 'done'" in message and "result is str" in message and "n_removed missing" in message


def test_an_unknown_version_or_kind_is_refused():
    with pytest.raises(ValueError, match="unknown schema 'display/2'"):
        check("display", {**DISPLAY, "schema": "display/2"})
    with pytest.raises(ValueError, match="unknown schema"):
        check("display", {**DISPLAY, "schema": "stage/1"})
    with pytest.raises(ValueError, match="not a JSON object"):
        check("display", [])


def test_periscope_skips_a_display_record_that_breaks_the_contract(tmp_path):
    import json
    from ecarsi.ui.registry import display_zones
    good, bad = tmp_path / "zones/c/d/run1", tmp_path / "zones/c/d/run2"
    good.mkdir(parents=True); bad.mkdir(parents=True)
    (good / "display.json").write_text(json.dumps({**DISPLAY, "name": "good"}))
    (bad / "display.json").write_text(json.dumps({**DISPLAY, "name": "bad", "work": None}))
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"display_root": str(tmp_path / "zones")}))
    assert set(display_zones(results)) == {"good"}


def test_a_receipt_names_one_of_three_endings():
    assert check("receipt", dict(state="failed", outputs=[], error="WorkerLost"))
    with pytest.raises(ValueError, match="state is 'timeout'"):
        check("receipt", dict(state="timeout", outputs=[]))


def test_a_turn_result_names_its_outcome_and_worker():
    worker = {"host": "sh03-01n29", "pid": 1}
    assert check("turn", dict(outcome="provider_error", response=None, worker=worker, error="APIConnectionError"))
    with pytest.raises(ValueError, match="outcome is 'ok'"):
        check("turn", dict(outcome="ok", worker=worker))
    with pytest.raises(ValueError, match="worker missing"):
        check("turn", dict(outcome="success", response={}))


def test_an_eca_pp_result_names_its_step_and_columns():
    assert check("eca-pp-identify-columns", dict(step="identify_columns", columns={}, sample_unit={"value": "whole"}))
    with pytest.raises(ValueError, match="step is 'standardize'"):
        check("eca-pp-identify-columns", dict(step="standardize", columns={}))
