import logging

import pytest
from rdm.gateway import policy
from rdm.gateway.data import DEFAULT_DENY_SERVERS, META_TOOL_DENY

PROJECT = "D:/Sources/proj"
ALLOW = ("codegraph_explore", "search")


def test_check_call_allows_listed_tool():
    assert policy.check_call("websearch", "search", ALLOW) == (True, "ok")


@pytest.mark.parametrize("tool", ["search", "fetch", "unknown_tool"])
def test_check_call_empty_or_absent_allowlist_denies_all(tool):
    for tool_allow in ((), None):
        assert policy.check_call("websearch", tool, tool_allow) == (False, "empty-allowlist")


def test_check_call_unknown_tool_denied():
    assert policy.check_call("websearch", "not_listed", ALLOW) == (False, "tool-not-allowed")


@pytest.mark.parametrize("tool", sorted(META_TOOL_DENY))
def test_check_call_meta_tool_denied_even_when_allowed(tool):
    tool_allow = (*META_TOOL_DENY, "search")
    assert policy.check_call("websearch", tool, tool_allow) == (False, "meta-tool-deny")


@pytest.mark.parametrize("server", sorted(DEFAULT_DENY_SERVERS))
def test_check_call_default_deny_server(server):
    assert policy.check_call(server, "search", ALLOW) == (False, "default-deny")


@pytest.mark.parametrize(
    "project_path",
    [
        "sub/dir",
        "sub/../other",
        PROJECT,
        PROJECT + "/sub",
        PROJECT + "\\sub",
        "D:\\Sources\\proj\\sub",
    ],
)
def test_check_call_codegraph_project_path_inside_allowed(project_path):
    allow, reason = policy.check_call(
        "codegraph", "codegraph_explore", ALLOW, {"projectPath": project_path}, PROJECT
    )
    assert (allow, reason) == (True, "ok")


@pytest.mark.parametrize(
    "project_path",
    [
        "../escape",
        "sub/../../escape",
        "E:/other",
        "D:/Sources",
        "D:/Sources/proj/../escape",
        "D:/SOURCES/proj",
        "d:/Sources/proj",
        "D:proj",
        "/Windows",
        "\\Windows",
        "D:/Sources/other-proj",
    ],
)
def test_check_call_codegraph_project_path_escape_denied(project_path):
    allow, reason = policy.check_call(
        "codegraph", "codegraph_explore", ALLOW, {"projectPath": project_path}, PROJECT
    )
    assert (allow, reason) == (False, "arg-outside-project")


def test_check_call_codegraph_project_path_absent_allowed():
    for args in (None, {}, {"query": "x"}):
        assert policy.check_call("codegraph", "codegraph_explore", ALLOW, args, PROJECT) == (True, "ok")


def test_check_call_codegraph_project_path_without_project_dir_denied():
    allow, reason = policy.check_call(
        "codegraph", "codegraph_explore", ALLOW, {"projectPath": "sub"}, None
    )
    assert (allow, reason) == (False, "arg-outside-project")


def test_check_call_codegraph_project_path_non_string_denied():
    allow, reason = policy.check_call(
        "codegraph", "codegraph_explore", ALLOW, {"projectPath": 42}, PROJECT
    )
    assert (allow, reason) == (False, "arg-outside-project")


def test_check_call_unconstrained_server_ignores_path_arg():
    allow, reason = policy.check_call(
        "websearch", "search", ALLOW, {"projectPath": "E:/anywhere"}, PROJECT
    )
    assert (allow, reason) == (True, "ok")


def test_exposed_tool_name_format():
    assert policy.exposed_tool_name("websearch", "search") == "websearch__search"
    assert policy.exposed_tool_name("codegraph", "codegraph_explore") == "codegraph__codegraph_explore"


@pytest.mark.parametrize(
    ("exposed", "parsed"),
    [
        ("websearch__search", ("websearch", "search")),
        ("codegraph__codegraph_explore", ("codegraph", "codegraph_explore")),
        ("a__b__c", ("a", "b__c")),
    ],
)
def test_parse_exposed_tool_name_inverse(exposed, parsed):
    assert policy.parse_exposed_tool_name(exposed) == parsed


@pytest.mark.parametrize("bad", ["", "no-separator", "websearch__", "__tool", "_"])
def test_parse_exposed_tool_name_unresolvable(bad):
    assert policy.parse_exposed_tool_name(bad) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("websearch", True),
        ("codegraph", True),
        ("a", True),
        ("a-b", True),
        ("a1-b2", True),
        ("web-", True),
        ("", False),
        ("A", False),
        ("web_search", False),
        ("-web", False),
        ("web search", False),
    ],
)
def test_valid_server_name(name, expected):
    assert policy.valid_server_name(name) is expected


def test_register_tools_identity_mapping():
    mapping = policy.register_tools(["websearch__search", "codegraph__codegraph_explore"])
    assert mapping == {
        "websearch__search": "websearch__search",
        "codegraph__codegraph_explore": "codegraph__codegraph_explore",
    }


def test_register_tools_duplicate_gets_deterministic_suffix():
    mapping = policy.register_tools(["srv__tool", "srv__tool", "srv__tool"])
    assert mapping == {
        "srv__tool": "srv__tool",
        "srv__tool_2": "srv__tool",
        "srv__tool_3": "srv__tool",
    }


def test_register_tools_name_over_64_chars_gets_suffix():
    entry = "srv__" + "t" * 60
    assert len(entry) == 65
    assert policy.register_tools([entry]) == {entry + "_2": entry}


def test_register_tools_name_exactly_64_chars_kept():
    entry = "srv__" + "t" * 59
    assert len(entry) == 64
    assert policy.register_tools([entry]) == {entry: entry}


def test_register_tools_deterministic_across_calls():
    entries = ["srv__tool", "srv__tool", "long__" + "t" * 60]
    assert policy.register_tools(entries) == policy.register_tools(entries)


def test_register_tools_skips_unresolvable_with_log(caplog):
    with caplog.at_level(logging.WARNING):
        mapping = policy.register_tools(["garbage", "also__ok"])
    assert mapping == {"also__ok": "also__ok"}
    assert any("garbage" in rec.getMessage() for rec in caplog.records)


def test_register_tools_skips_invalid_server_name_with_log(caplog):
    with caplog.at_level(logging.WARNING):
        mapping = policy.register_tools(["Bad_Name__tool"])
    assert mapping == {}
    assert any("Bad_Name__tool" in rec.getMessage() for rec in caplog.records)
