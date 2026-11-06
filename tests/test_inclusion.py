"""The cross-sample inclusion decision is validated before it is accepted."""
import pytest

from ecarsi.stages import inclusion


@pytest.mark.parametrize('value',[1,'false',None])
def test_inclusion_requires_boolean(value):
    with pytest.raises(ValueError):
        inclusion.validate_inclusion({'notes':'ok','samples':[{'sample':'S1','include':value,'reason':'ok'}]},['S1'])


def test_an_excluded_sample_names_one_of_two_reasons():
    rows = [{'sample': 'S1', 'include': True, 'reason': 'ok'}, {'sample': 'S2', 'include': False, 'reason': 'shattered'}]
    with pytest.raises(ValueError, match='exclude_reason'):
        inclusion.validate_inclusion({'notes': 'ok', 'samples': rows}, ['S1', 'S2'])
    rows[1]['exclude_reason'] = 'few cells'
    with pytest.raises(ValueError, match='exclude_reason'):
        inclusion.validate_inclusion({'notes': 'ok', 'samples': rows}, ['S1', 'S2'])
    for reason in inclusion.EXCLUDE_REASONS:
        rows[1]['exclude_reason'] = reason
        inclusion.validate_inclusion({'notes': 'ok', 'samples': rows}, ['S1', 'S2'])
