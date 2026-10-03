"""ecarsi.cost.record: an agent call's spend and tokens become one `cost` event in progress.log."""
from __future__ import annotations

from ecarsi import cost, layout as L


def test_record_writes_usd_and_tokens_and_skips_an_empty_report(tmp_path):
    unit = tmp_path / "unit"
    cost.record(unit, "round1/crosssample", 1.5, "inspect")
    cost.record(unit, "persample/sampleA", None, "qc", tokens_in=1000, tokens_out=200)
    cost.record(unit, "step", None)  # neither: nothing to record
    events = [event for _, event in L.read_log(unit)]
    assert events == ["cost step=round1/crosssample usd=1.5000 label=inspect",
                      "cost step=persample/sampleA tokens_in=1000 tokens_out=200 label=qc"]
