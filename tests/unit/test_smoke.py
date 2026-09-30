import importlib.util

import pytest


def test_rdm_package_layout():
    spec = importlib.util.find_spec("rdm")
    if spec is None:
        pytest.skip("home/rdm package does not exist yet")
    assert spec.origin is not None
