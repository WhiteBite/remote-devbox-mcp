import sys
from pathlib import Path

import psutil
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _reap_spawned_children():
    yield
    try:
        children = psutil.Process().children(recursive=True)
    except psutil.Error:
        return
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            continue
    try:
        psutil.wait_procs(children, timeout=5)
    except psutil.Error:
        return
