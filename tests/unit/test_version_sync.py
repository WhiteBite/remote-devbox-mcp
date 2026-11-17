import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _pyproject_version() -> str:
    section = re.search(r"^\[project\](.*?)(?=^\[|\Z)", _read("pyproject.toml"), re.MULTILINE | re.DOTALL)
    assert section, "pyproject.toml has no [project] table"
    version = re.search(r'^version\s*=\s*"([^"]+)"', section.group(1), re.MULTILINE)
    assert version, "pyproject.toml has no project version"
    return version.group(1)


def _manifest_version() -> str:
    return json.loads(_read("plugin.json"))["version"]


def test_pyproject_version_matches_plugin_json() -> None:
    assert _pyproject_version() == _manifest_version()