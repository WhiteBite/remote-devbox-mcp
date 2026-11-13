from rdm import envschema

ENV = {
    "INGRESS_MODE": "relay",
    "RELAY_SUBDOMAIN": "ide-account-manager",
    "TUNNEL_TOKEN": "eyJhbGciOiJIUzI1NiJ9.token-value",
    "PUBLIC_URL": "https://devbox.example.test",
    "EGRESS_CONTAINER": "rdm-egress",
    "VLESS_SUB_URL": "https://subs.example.test/abc",
    "JOB_TIMEOUT_SECONDS": "1800",
    "OPENCODE_MCP_PERMISSIONS": '{"bash":"allow"}',
    "TUNNEL_TAIL": "run --token secret",
    "MCP_BEARER_TOKEN": "b" * 64,
    "MCP_PUBLIC_TOKEN": "p" * 64,
    "INGRESS_TOKEN": "i" * 64,
    "GATEWAY_TOKEN": "g" * 64,
}


def _row(rows: list[dict[str, object]], key: str) -> dict[str, object]:
    return next(row for row in rows if row["key"] == key)


def test_entries_mask_secrets_and_mark_readonly() -> None:
    rows = envschema.entries(ENV)
    tunnel = _row(rows, "TUNNEL_TOKEN")
    assert tunnel["secret"] is True
    assert "..." in str(tunnel["value"])
    assert "token-value" not in str(tunnel["value"])
    assert _row(rows, "INGRESS_MODE")["value"] == "relay"
    assert _row(rows, "INGRESS_MODE")["readonly"] is False
    assert _row(rows, "MCP_BEARER_TOKEN")["readonly"] is True
    assert _row(rows, "OPENCODE_MCP_PERMISSIONS")["readonly"] is True


def test_entries_cover_every_registered_key() -> None:
    rows = envschema.entries({})
    assert {row["key"] for row in rows} == {
        "INGRESS_MODE",
        "RELAY_SUBDOMAIN",
        "TUNNEL_TOKEN",
        "PUBLIC_URL",
        "EGRESS_CONTAINER",
        "VLESS_SUB_URL",
        "JOB_TIMEOUT_SECONDS",
        "OPENCODE_MCP_PERMISSIONS",
        "TUNNEL_TAIL",
        "MCP_BEARER_TOKEN",
        "MCP_PUBLIC_TOKEN",
        "INGRESS_TOKEN",
        "GATEWAY_TOKEN",
    }


def test_apply_patch_accepts_valid_change_and_reports_restart() -> None:
    new_map, errors, restart = envschema.apply_patch(ENV, {"INGRESS_MODE": "cloudflare"})
    assert errors == []
    assert restart == ["ingress"]
    assert new_map["INGRESS_MODE"] == "cloudflare"


def test_apply_patch_unchanged_value_requires_no_restart() -> None:
    new_map, errors, restart = envschema.apply_patch(ENV, {"INGRESS_MODE": "relay"})
    assert errors == []
    assert restart == []
    assert new_map == ENV


def test_apply_patch_rejects_bad_enum() -> None:
    new_map, errors, restart = envschema.apply_patch(ENV, {"INGRESS_MODE": "tunnel"})
    assert errors == ["INGRESS_MODE: expected one of: '', 'cloudflare', 'relay'"]
    assert restart == []
    assert new_map == ENV


def test_apply_patch_rejects_bad_subdomain() -> None:
    _, errors, _ = envschema.apply_patch(ENV, {"RELAY_SUBDOMAIN": "Bad Name"})
    assert errors and errors[0].startswith("RELAY_SUBDOMAIN:")


def test_apply_patch_rejects_bad_url() -> None:
    _, errors, _ = envschema.apply_patch(ENV, {"PUBLIC_URL": "devbox.example.test"})
    assert errors and errors[0].startswith("PUBLIC_URL:")


def test_apply_patch_job_timeout_bounds() -> None:
    _, too_small, _ = envschema.apply_patch(ENV, {"JOB_TIMEOUT_SECONDS": "10"})
    assert too_small
    _, at_max, restart = envschema.apply_patch(ENV, {"JOB_TIMEOUT_SECONDS": "3600"})
    assert at_max == []
    assert restart == ["stack"]


def test_apply_patch_rejects_readonly_and_unknown_keys() -> None:
    _, errors, restart = envschema.apply_patch(
        ENV, {"MCP_BEARER_TOKEN": "x" * 64, "NOT_A_KEY": "1"}
    )
    assert len(errors) == 2
    assert any("managed key" in error for error in errors)
    assert any("unknown key" in error for error in errors)
    assert restart == []


def test_apply_patch_reports_every_error_and_keeps_map() -> None:
    new_map, errors, restart = envschema.apply_patch(
        ENV, {"INGRESS_MODE": "nope", "EGRESS_CONTAINER": "rdm-warp"}
    )
    assert len(errors) == 2
    assert new_map == ENV
    assert restart == []