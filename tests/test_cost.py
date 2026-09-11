"""ecarsi.cost: dollar and token usage events round-trip through progress.log,
and both the claude ($) and openai (tokens) subprocess log-line formats parse."""
from __future__ import annotations

from ecarsi import cost


def test_record_and_summarize_usd_only(tmp_path):
    unit = tmp_path / "unit"
    cost.record(unit, "round1/crosssample", 1.5, "inspect")
    cost.record(unit, "round1/crosssample", 0.5, "annotate")
    s = cost.summarize(unit)
    assert s["total"] == 2.0 and s["tokens_in"] == 0 and s["tokens_out"] == 0
    assert s["by_step"]["round1/crosssample"]["n"] == 2


def test_record_and_summarize_tokens_only(tmp_path):
    unit = tmp_path / "unit"
    cost.record(unit, "persample/sampleA", None, "qc", tokens_in=1000, tokens_out=200)
    s = cost.summarize(unit)
    assert s["total"] == 0.0 and s["tokens_in"] == 1000 and s["tokens_out"] == 200


def test_record_with_neither_is_a_noop(tmp_path):
    unit = tmp_path / "unit"
    cost.record(unit, "step", None)
    assert cost.summarize(unit)["n"] == 0


def test_token_re_matches_openai_log_line():
    line = ("== [inspect] HARNESS=openai api=responses server_state=on model=doubao-seed-2-1-turbo-260628 "
            "run: 3 model request(s), 4200 input / 380 output tokens (0 reasoning)")
    m = cost.TOKEN_RE.search(line)
    assert m is not None
    assert m.group("label") == "inspect"
    assert m.group("tin") == "4200" and m.group("tout") == "380"


def test_cost_re_matches_claude_log_line():
    line = "== [annotate] agent cost: $0.42"
    m = cost.COST_RE.search(line)
    assert m is not None
    assert m.group("label") == "annotate" and m.group("usd") == "0.42"


def test_summary_md_reports_both_when_present(tmp_path):
    unit = tmp_path / "unit"
    cost.record(unit, "step", 1.0, tokens_in=10, tokens_out=5)
    md = "\n".join(cost.summary_md(unit))
    assert "$1.00" in md and "10" in md and "5" in md
