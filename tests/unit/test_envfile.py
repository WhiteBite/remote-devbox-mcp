import os
from pathlib import Path

import pytest
from rdm.envfile import EnvFile


def test_preserves_comments_and_order(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = (
        "# devbox env\n"
        "PROJECT_DIR=C:/work/muffin\n"
        "\n"
        "# tokens\n"
        "MCP_BEARER_TOKEN=abc\n"
        "INGRESS_TOKEN=def\n"
    )
    path.write_bytes(original.encode("utf-8"))
    env = EnvFile.load(path)
    assert env.render() == original
    env.set("MCP_BEARER_TOKEN", "rotated")
    assert env.render() == (
        "# devbox env\n"
        "PROJECT_DIR=C:/work/muffin\n"
        "\n"
        "# tokens\n"
        "MCP_BEARER_TOKEN=rotated\n"
        "INGRESS_TOKEN=def\n"
    )


def test_preserves_unknown_manual_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(
        b"PROJECT_DIR=C:/work/muffin\n"
        b"MANUAL_FLAG=1\n"
        b"export SHELL_VAR=keepme\n"
        b"EXTRA_TOOL=x=y\n"
    )
    env = EnvFile.load(path)
    assert env.get("MANUAL_FLAG") == "1"
    assert env.get("EXTRA_TOOL") == "x=y"
    assert env.get("SHELL_VAR") is None
    env.set("PROJECT_DIR", "C:/work/other")
    assert env.render() == (
        "PROJECT_DIR=C:/work/other\n"
        "MANUAL_FLAG=1\n"
        "export SHELL_VAR=keepme\n"
        "EXTRA_TOOL=x=y\n"
    )


def test_updates_existing_key_in_place(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"PROJECT_DIR=old\nTOOLCHAIN=java21\nGIT_NAME=Old\n")
    env = EnvFile.load(path)
    env.set("TOOLCHAIN", "flutter:3.44.9")
    assert env.get("TOOLCHAIN") == "flutter:3.44.9"
    assert env.render() == "PROJECT_DIR=old\nTOOLCHAIN=flutter:3.44.9\nGIT_NAME=Old\n"


def test_new_key_appended_in_canonical_order(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"MCP_BEARER_TOKEN=abc\n")
    env = EnvFile.load(path)
    env.set("TUNNEL_TOKEN", "eyJtok")
    env.set("PROJECT_DIR", "C:/work/muffin")
    env.set("GIT_NAME", "agent")
    env.set("Z_MANUAL", "zz")
    env.set("A_MANUAL", "aa")
    assert env.get("TUNNEL_TOKEN") is not None
    assert env.get("PROJECT_DIR") == "C:/work/muffin"
    assert env.render() == (
        "MCP_BEARER_TOKEN=abc\n"
        "PROJECT_DIR=C:/work/muffin\n"
        "GIT_NAME=agent\n"
        "TUNNEL_TOKEN=eyJtok\n"
        "A_MANUAL=aa\n"
        "Z_MANUAL=zz\n"
    )


def test_atomic_write_no_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"OLD=1\n")
    env = EnvFile.load(path)
    env.set("NEW_KEY", "value")

    def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        assert Path(src).parent == tmp_path
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        env.write(path)
    assert path.read_bytes() == b"OLD=1\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_crlf_normalized_to_lf(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"PROJECT_DIR=C:/work\r\nTOOLCHAIN=java21\r\n")
    env = EnvFile.load(path)
    env.set("TOOLCHAIN", "flutter:3.44.9")
    env.write(path)
    assert path.read_bytes() == b"PROJECT_DIR=C:/work\nTOOLCHAIN=flutter:3.44.9\n"


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    env = EnvFile.load(tmp_path / "missing.env")
    assert env.get("ANY_KEY") is None
    assert env.get("ANY_KEY", "fallback") == "fallback"
    assert env.get("ANY_KEY") is None
    assert env.render() == ""


def test_value_containing_equals_preserved(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"SETUP_SCRIPT_B64=  aGVsbG8=d29ybGQ=  \n")
    env = EnvFile.load(path)
    assert env.get("SETUP_SCRIPT_B64") == "aGVsbG8=d29ybGQ="
    env.set("TUNNEL_TOKEN", "eyJ=raw=payload")
    assert env.render() == "SETUP_SCRIPT_B64=  aGVsbG8=d29ybGQ=  \nTUNNEL_TOKEN=eyJ=raw=payload\n"


def test_remove_deletes_key_line(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"PROJECT_DIR=C:/work\nTOOLCHAIN=java21\nGIT_NAME=agent\n")
    env = EnvFile.load(path)
    env.remove("TOOLCHAIN")
    env.remove("ABSENT_KEY")
    assert env.get("TOOLCHAIN") is None
    assert env.get("TOOLCHAIN") is None
    assert env.render() == "PROJECT_DIR=C:/work\nGIT_NAME=agent\n"
