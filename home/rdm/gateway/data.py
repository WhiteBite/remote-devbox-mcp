DEFAULT_DENY_SERVERS = frozenset({"stitch-manager", "lgm", "ssh", "playwright"})
META_TOOL_DENY = frozenset(
    {
        "execute_any",
        "list_mcp_commands",
        "import_accounts_payload",
        "bulk_export_accounts",
        "import_secrets",
    }
)
NETWORK_EGRESS_SERVERS = frozenset({"websearch", "context7"})
SECRET_TOUCHING_SERVERS = frozenset(
    {"stitch-manager", "lgm", "ssh", "playwright", "stitch-totp", "stitch-mail"}
)
ARG_CONSTRAINTS = {"codegraph": {"projectPath": "project_dir"}}
