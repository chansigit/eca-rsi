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


def test_backend_re_matches_the_bridge_line_for_any_backend():
    line = "09-10 12:00:00 == [annotate] resolved backend: harness=openai model=doubao-seed-2-1-turbo-260628"
    m = cost.BACKEND_RE.search(line)
    assert m is not None
    assert m.group("label") == "annotate" and m.group("harness") == "openai"
    assert m.group("model") == "doubao-seed-2-1-turbo-260628"


def test_scan_line_records_backend_without_double_counting_cost(tmp_path):
    # a real subprocess prints both lines for one agent call, on separate lines
    unit = tmp_path / "unit"
    cost._scan_line(unit, "round01/crosssample", "== [inspect] resolved backend: harness=claude model=claude-sonnet-5")
    cost._scan_line(unit, "round01/crosssample", "== [inspect] agent cost: $0.10")
    s = cost.summarize(unit)
    assert s["n"] == 1  # only the cost line counts as a spend event
    assert cost.backend_events(unit) == [{"time": cost.backend_events(unit)[0]["time"], "step": "round01/crosssample",
                                           "harness": "claude", "model": "claude-sonnet-5", "label": "inspect"}]


def test_round_backends_groups_by_round_and_dedupes(tmp_path):
    unit = tmp_path / "unit"
    cost.record_backend(unit, "round01/crosssample", "claude", "claude-sonnet-5", "inspect")
    cost.record_backend(unit, "round01/crosssample", "claude", "claude-sonnet-5", "annotate")  # same config, dedupes
    cost.record_backend(unit, "round01/zoomin", "openai", "doubao-seed-2-1-turbo-260628", "zmip")
    cost.record_backend(unit, "persample/sampleA", "claude", "claude-sonnet-5", "identify")
    by_round = cost.round_backends(unit)
    assert by_round == {
        "round01": ["claude:claude-sonnet-5", "openai:doubao-seed-2-1-turbo-260628"],
        "front": ["claude:claude-sonnet-5"],
    }


def test_backend_summary_md_reports_a_mixed_round_without_flagging_it(tmp_path):
    unit = tmp_path / "unit"
    cost.record_backend(unit, "round01/crosssample", "claude", "claude-sonnet-5")
    cost.record_backend(unit, "round01/zoomin", "openai", "doubao-seed-2-1-turbo-260628")
    md = "\n".join(cost.backend_summary_md(unit))
    assert "claude:claude-sonnet-5" in md and "openai:doubao-seed-2-1-turbo-260628" in md
    assert "round01" in md


def test_backend_summary_md_says_so_when_nothing_reported(tmp_path):
    unit = tmp_path / "unit"
    md = "\n".join(cost.backend_summary_md(unit))
    assert "no backend reported" in md
