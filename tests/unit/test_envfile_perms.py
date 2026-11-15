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
    principals = [line.split(":(")[0].rsplit(" ", 1)[-1] for line in ace_lines]
    owner_aces = [
        line for line, principal in zip(ace_lines, principals, strict=False) if user.lower() in principal.lower()
    ]
    assert len(owner_aces) == 1
    assert "(F)" in owner_aces[0]
    shared = [
        principal
        for principal in principals
        if principal.lower().endswith(("\\users", "\\everyone"))
    ]
    assert shared == []
