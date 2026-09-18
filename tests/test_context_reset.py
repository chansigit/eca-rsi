"""A session whose transcript the provider rejects restarts as a fresh conversation with the
host state carried over (Eye round 5, 2026-09-16: 37 turns, 4.5 M input tokens, HTTP 400 x3)."""
from ecarsi.agent.session import RESET_NOTE, reference, reset_spec
from ecarsi.warm_pool.state import save


def test_reset_spec_renames_nests_explains_and_carries_the_reached_state(tmp_path):
    for name, value in (('initial.json', {'step': 0}), ('state.json', {'step': 9})):
        save(tmp_path / name, value)
    save(tmp_path / 'out.json', {'state': reference(tmp_path / 'state.json')})
    spec = dict(session_id='cross-abc', output_root=str(tmp_path / 'type-1'), prompt='base', tools=['t'],
                tool_state=reference(tmp_path / 'initial.json'), max_turns=80)
    context = {'results': [{'output': reference(tmp_path / 'out.json')}]}
    fresh = reset_spec(spec, context, 2)
    assert fresh['session_id'] == 'cross-abc-g2' and fresh['output_root'] == str(tmp_path / 'type-1' / 'generation-2')
    assert fresh['prompt'] == 'base' + RESET_NOTE and fresh['tool_state'] == reference(tmp_path / 'state.json')
    assert {k: v for k, v in fresh.items() if k not in {'session_id', 'output_root', 'prompt', 'tool_state'}} == {'tools': ['t'], 'max_turns': 80}
    third = reset_spec(fresh, {'tool_state': reference(tmp_path / 'initial.json')}, 3)
    assert third['session_id'] == 'cross-abc-g3' and third['output_root'] == str(tmp_path / 'type-1' / 'generation-3')
    assert third['prompt'].count(RESET_NOTE) == 2 and third['tool_state'] == reference(tmp_path / 'initial.json')
    del spec['tool_state']
    assert 'tool_state' not in reset_spec(spec, context, 2)
