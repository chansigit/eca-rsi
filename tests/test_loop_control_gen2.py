"""The manual gearbox, generation 2: the round policy a dataset was admitted with lives in its
immutable workflow spec, so loop_control.json is the one thing allowed to move it mid-run."""
import json
from pathlib import Path

import pytest

from ecarsi import index, round_policy


def control(unit: Path, **values):
    unit.mkdir(parents=True, exist_ok=True)
    (unit / round_policy.CONTROL_FILE).write_text(json.dumps(values))
    return unit


def test_the_reader_is_shared_by_both_generations_and_never_fails_a_run(tmp_path):
    unit = control(tmp_path / 'u', cap=20, rounds=None, pause=True)
    assert round_policy.read_control(unit) == {'cap': 20, 'rounds': None, 'pause': True}
    assert round_policy.read_control(tmp_path / 'absent') == {}
    # generation 1 keeps its name and its progress.log reporting, reading the same file
    from ecarsi import loop
    assert loop.read_control(unit) == {'cap': 20, 'rounds': None, 'pause': True}
    for bad, why in [('[]', 'not a JSON object'), ('{"nope": 1}', 'unknown key'),
                     ('{"cap": 0}', 'must be >= 1'), ('{"cap": true}', 'must be an integer'),
                     ('{"pause": 1}', 'pause must be a boolean'), ('not json', 'Expecting')]:
        (unit / round_policy.CONTROL_FILE).write_text(bad)
        said = []
        assert round_policy.read_control(unit, on_error=said.append) == {}   # ignored, not raised
        assert why in said[0] and said[0].startswith('loop_control.json ignored')


def test_a_control_moves_the_limit_and_a_pause_outranks_a_release(tmp_path):
    """The decision is taken once per round, and that is when the file is read -- so a cap can be
    raised on a run about to hit it, and a stop can be ordered on one about to release."""
    admitted = {'rounds': None, 'cap': 3, 'extra_rounds_after_convergence': 0, 'max_removed': 1000}
    unit = tmp_path / 'units' / 'u'
    read = lambda: round_policy.read_control(unit)
    call = lambda stats: round_policy.decide_with_control(len(stats), stats, admitted, read())

    stats = [dict(n_in=100, n_out=90, removed=10, frac=.10), dict(n_in=90, n_out=81, removed=9, frac=.10)]
    busy = stats + [dict(n_in=81_000, n_out=79_000, removed=2_000, frac=.0247)]   # over every rule
    assert call(busy) == ('release', 'FORCED: safety cap 3 rounds reached')       # only the cap ends it
    control(unit, cap=9)                                                          # moved while it runs
    assert call(busy) == ('continue', 'removed 2.47% (2000 cells)')
    assert round_policy.resolve(admitted, read())['cap'] == 9                     # and the round records it
    assert round_policy.resolve(admitted, read())['max_removed'] == 1000          # untouched keys stand

    done = stats + [dict(n_in=81_000, n_out=81_000, removed=0, frac=0.0)]
    assert call(done)[0] == 'release'
    control(unit, cap=9, stop_after_round=len(done))     # a stop beats the release the rule wanted
    decision, why = call(done)
    assert decision == 'pause' and why.startswith('PAUSED: loop_control stopped the unit after round 3')
    control(unit, cap=9, pause=True)                     # and pause needs no round number
    assert call(busy)[0] == 'pause'

    # only the four decision limits are overridable; the rest of the file is about stopping
    control(unit, pause_after_stage='zoomin')
    assert round_policy.resolve(admitted, read()) == admitted
    assert read() == {'pause_after_stage': 'zoomin'}      # read, so the caller can say it cannot obey


def test_the_activity_uses_that_one_decision_and_stops_the_workflow_with_it():
    """The round activity must not re-implement the overlay, and a paused round has to reach the
    workflow as a failure -- that is the only stop `resume-dataset` already knows how to continue."""
    source = (Path(__file__).parent.parent / 'ecarsi' / 'control' / 'dataset.py').read_text()
    assert 'decide_with_control(n, stats, spec[\'round_policy\'], control)' in source
    assert "resolve(spec['round_policy'], control)" in source
    assert 'read_control(unit_root, on_error=notes.append)' in source
    assert "pause_after_stage is not supported" in source      # said out loud, not half-applied
    assert "result['paused'] = reason" in source
    assert "raise ApplicationError(progress['paused'], non_retryable=True)" in source
    # the round itself still publishes, with its policy and the control it obeyed
    assert "**({'control': control} if control else {})" in source


def test_a_paused_unit_reads_as_held_not_broken(tmp_path):
    """The workflow fails so the unit stays resumable, but a run waiting on a person is not a
    run that broke: it gets the queued/paused colour, and the dataset says why."""
    from tests.test_gen2_pages import gen2_run, save
    root = gen2_run(tmp_path / 'g2', released=False)
    save(root / 'publication.json', {'state': 'incomplete', 'dataset_id': 'D', 'units': [],
         'failed_units': [{'unit': 'u', 'error': 'PAUSED: loop_control stopped the unit after round 2'}],
         'n_input': 100, 'n_survived': 0, 'n_removed': 0, 'forced_release': False})
    state = index.unit_state(root / 'units' / 'u')
    assert state['stage_class'] == 'paused'
    assert state['stage'].startswith('paused — loop_control stopped the unit after round 2')
    assert index.dataset_state(root)['cls'] == 'paused'
    assert '.queued,.paused{--st:var(--wait)' in index.CSS      # held, in the waiting colour
