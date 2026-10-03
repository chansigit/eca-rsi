"""The cross-sample inclusion decision is validated before it is accepted."""
import pytest

from ecarsi import crosssample


@pytest.mark.parametrize('value',[1,'false',None])
def test_inclusion_requires_boolean(value):
    with pytest.raises(ValueError):
        crosssample.validate_inclusion({'notes':'ok','samples':[{'sample':'S1','include':value,'reason':'ok'}]},['S1'])
