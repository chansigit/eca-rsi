from harness_bridge.logtimes import durations, report

LOG = """09-06 17:00:00 == normalize/log1p
09-06 17:00:30 == harmony (harmonypy 2.0.0, 8 thread(s))
[s1] 09-06 17:02:00 == neighbors (use_rep=X_pca_harmony)
09-06 17:02:10 == [msp inspect] agent: check_deg(0)
09-06 17:02:14 == [msp inspect] check_deg took 3.4 s
09-06 17:02:15 == [msp inspect] time: wall 12 s, tools 3.4 s in 1 call(s)
"""


def test_gaps_and_took_lines(capsys):
    gaps = durations(LOG.splitlines())
    assert [round(g) for g, _ in gaps] == [30, 90, 10, 4, 1]
    report(LOG.splitlines())
    out = capsys.readouterr().out
    assert "90 s  harmony" in out and "30 s  normalize/log1p" in out
    assert "3.4 s  check_deg ×1" in out
    assert "agent:" not in out.split("tool calls")[0]
