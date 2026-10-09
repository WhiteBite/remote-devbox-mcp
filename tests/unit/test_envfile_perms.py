import os
import subprocess

import pytest
from rdm.envfile import EnvFile


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_write_sets_owner_only_mode_on_posix(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"MCP_BEARER_TOKEN=abc\n")
    os.chmod(path, 0o644)
    env = EnvFile.load(path)
    env.set("MCP_BEARER_TOKEN", "rotated")
    env.write(path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="icacls owner-only ACL")
def test_write_grants_owner_only_acl_on_windows(tmp_path):
    path = tmp_path / ".env"
    env = EnvFile.load(path)
    env.set("MCP_BEARER_TOKEN", "abc")
    env.write(path)
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    user = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
    ace_lines = [line for line in listing.splitlines() if ":(" in line]
    assert len(ace_lines) == 1
    assert user.lower() in ace_lines[0].lower()
    assert "(F)" in ace_lines[0]
