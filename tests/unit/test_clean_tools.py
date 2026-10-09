import json
import subprocess
from pathlib import Path

from rdm import clean_tools


def _volume(tmp_path, entries):
    root = tmp_path / "tools"
    root.mkdir()
    for meta in entries.values():
        (root / Path(meta["path"]).name).mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return root


def test_unreferenced_lists_zero_refcount_sorted():
    registry = {"entries": {"b": {"refcount": 0}, "a": {"refcount": 0}, "c": {"refcount": 2}}}
    assert clean_tools.unreferenced(registry) == ["a", "b"]


def test_clean_volume_removes_only_unreferenced(tmp_path):
    root = _volume(tmp_path, {
        "java21": {"refcount": 1, "path": "/opt/tools/java21"},
        "flutter:3.44.9": {"refcount": 0, "path": "/opt/tools/flutter-3.44.9"},
    })
    assert clean_tools.clean_volume(root) == ["flutter:3.44.9"]
    assert (root / "java21").is_dir()
    assert not (root / "flutter-3.44.9").exists()
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert list(registry["entries"]) == ["java21"]


def test_clean_volume_idempotent(tmp_path):
    root = _volume(tmp_path, {"old": {"refcount": 0, "path": "/opt/tools/old"}})
    assert clean_tools.clean_volume(root) == ["old"]
    assert clean_tools.clean_volume(root) == []


def test_clean_volume_without_registry(tmp_path):
    assert clean_tools.clean_volume(tmp_path) == []


def test_clean_volume_skips_unsafe_paths(tmp_path):
    root = _volume(tmp_path, {"x": {"refcount": 0, "path": "/opt/tools/../escape"}})
    assert clean_tools.clean_volume(root) == []
    assert (root / "escape").is_dir()


def _fake_docker(registry_text, fail_rm=False):
    calls = []

    def runner(*args, input=None, timeout=120):
        calls.append((args, input))
        if "cat" in args and input is None:
            return subprocess.CompletedProcess(list(args), 0, stdout=registry_text, stderr="")
        if "rm" in args and fail_rm:
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="busy")
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    return runner, calls


def test_clean_tools_docker_flow(tmp_path, capsys):
    registry = {"entries": {
        "java21": {"refcount": 1, "path": "/opt/tools/java21"},
        "node20": {"refcount": 0, "path": "/opt/tools/node20"},
    }}
    runner, calls = _fake_docker(json.dumps(registry))
    assert clean_tools.clean_tools(runner=runner) == 0
    rm_args = [c[0] for c in calls if "rm" in c[0]][0]
    assert "/opt/tools/node20" in rm_args
    assert "/opt/tools/java21" not in rm_args
    write_input = [c[1] for c in calls if c[1] is not None][0]
    assert "node20" not in json.loads(write_input)["entries"]
    assert "java21" in json.loads(write_input)["entries"]
    assert "удалён node20" in capsys.readouterr().out


def test_clean_tools_no_registry_is_noop(capsys):
    def runner(*args, input=None, timeout=120):
        return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="No such file")

    assert clean_tools.clean_tools(runner=runner) == 0
    assert "чистить нечего" in capsys.readouterr().out


def test_clean_tools_rm_failure_keeps_registry(tmp_path):
    registry = {"entries": {"x": {"refcount": 0, "path": "/opt/tools/x"}}}
    runner, calls = _fake_docker(json.dumps(registry), fail_rm=True)
    assert clean_tools.clean_tools(runner=runner) == 1
    assert [c for c in calls if c[1] is not None] == []
