"""osp.api is eca-rsi's contract with osp: every name in it must resolve."""
from osp import api


def test_every_public_name_resolves():
    for name in api.__all__:
        assert getattr(api, name) is not None, name
