import json
from pathlib import Path

from rdm.ps_import import parse_profile_ps1

REPO = Path(__file__).resolve().parents[2]
PROJECTS = REPO / "projects"
FIXTURES = REPO / "tests" / "fixtures"


def test_ps_import_muffin_roundtrip():
    parsed = parse_profile_ps1((FIXTURES / "muffin.ps1").read_text(encoding="utf-8"))
    expected = json.loads((PROJECTS / "muffin.json").read_text(encoding="utf-8"))

    assert parsed == expected


def test_ps_import_dollar_true_false():
    parsed = parse_profile_ps1("$Background = $true\n$Required = $false")

    assert parsed == {"background": True, "required": False}


def test_ps_import_case_insensitive_keys():
    upper = parse_profile_ps1("$HostServices = @( @{ Port = 8792; CMD = 'x' } )")
    lower = parse_profile_ps1("$HostServices = @( @{ port = 8792; cmd = 'x' } )")

    assert upper == lower
    assert upper == {"host_services": [{"port": 8792, "cmd": "x"}]}