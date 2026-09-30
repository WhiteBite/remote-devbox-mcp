import json
from pathlib import Path

import pytest

from rdm.profiles import load

REPO = Path(__file__).resolve().parents[2]
PROJECTS = REPO / "projects"


def test_load_rejects_unknown_key(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"project_dir": "x", "bogus": 1}), encoding="utf-8")

    with pytest.raises(ValueError):
        load(path)


def test_load_muffin_json():
    profile = load(PROJECTS / "muffin.json")

    assert profile.project_dir == "d:/Sources/StartUp/Muffin"
    assert profile.toolchain == "java21 flutter:3.44.9"
    assert profile.git_name == "WhiteBite"
    assert profile.git_email == "ad.lord9000@yandex.ru"
    assert profile.preview_origin == "http://host.docker.internal:8080"
    assert profile.allowed_ports == (8765, 8080)
    assert profile.deny_mounts == (".env.staging", "apps/backend/.env")
    assert profile.mode == "standard"
    assert profile.runner_commands == ()

    assert len(profile.host_services) == 1
    service = profile.host_services[0]
    assert service.port == 8792
    assert service.auth == "bearer"
    assert service.cwd == "D:\\Sources\\StartUp\\Muffin"
    assert service.cmd == "python tools/muffin-supervisor/server.py"