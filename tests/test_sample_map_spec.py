"""The dataset spec's organize.sample_map: checked when the dataset is submitted, pinned by content, and
handed to organize's execute step."""
import json
from pathlib import Path

import pytest

from ecarsi.control.coordinator import submit_execute, validate_sample_map


def test_the_sample_map_is_checked_before_anything_runs():
    validate_sample_map({"batch_key": False})
    validate_sample_map({"sources": {}, "merges": [], "exclude_cells": [], "batch_key": "mouse.id"})
    for bad, message in [([], "JSON object"), ({"nope": 1}, "unknown sample-map key"), ({"merges": {}}, "lists"),
                         ({"batch_key": True}, "or false"), ({"batch_key": " "}, "or false")]:
        with pytest.raises(ValueError, match=message):
            validate_sample_map(bad)


def test_execute_pins_the_sample_map_and_passes_it_on(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("ecarsi.warm_pool.state.submit", lambda root, request: sent.append(request))
    prepared, reply = tmp_path / "prepared.json", tmp_path / "reply.json"
    prepared.write_text('{"records": []}')
    reply.write_text("{}")
    (tmp_path / "run").mkdir()
    spec = dict(run_id="r1-organize", output_root=str(tmp_path / "run" / "00-organize"), pool_root=str(tmp_path / "pool"),
                execute_cpus=1, execute_memory_mb=64, execute_timeout_seconds=10, sample_map={"batch_key": False})
    submit_execute(spec, str(prepared), str(reply))
    request, = sent
    path = request["args"][request["args"].index("--sample-map") + 1]
    assert json.loads(Path(path).read_text()) == {"batch_key": False}
    assert path in [item["path"] for item in request["inputs"]]
