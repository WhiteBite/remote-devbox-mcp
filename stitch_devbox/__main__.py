"""RPC entry point for the stitch-devbox service plugin.

Spawned by ``ServicePluginHost`` as ``python -m stitch_devbox``.
Implements the JSON-RPC 2.0 line protocol via ``RpcPluginServer``.

Read commands mirror the cockpit payload builders; write commands enqueue
the devbox CLI entry points on the single-flight action queue (see
``service.py``).  No plugin-local storage: state lives in the devbox repo
(``.env``, profiles, events, logs).
"""

# _generated_by: stitch_plugin_tools scaffold v3

from __future__ import annotations

from typing import Any

from . import service

try:
    from autoreg.plugin.rpc import RpcPluginServer
except ImportError:
    from ._vendor.rpc_server import RpcPluginServer


class _Ctx:
    """Mutable container for plugin.init handshake state."""

    db_path: str = ""
    data_dir: str = ""
    supported: list[str] = []


ctx = _Ctx()


def _handle_init(params: dict[str, Any]) -> dict[str, Any]:
    ctx.db_path = str(params.get("db_path", ""))
    ctx.data_dir = str(params.get("data_dir", ""))
    supported = params.get("supported")
    ctx.supported = list(supported) if isinstance(supported, list) else []
    return {
        "plugin_id": params.get("plugin_id", ""),
        "db_path": ctx.db_path,
        "data_dir": ctx.data_dir,
        "capabilities": [],
    }


def _handle_migrate_db(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "from_version": params.get("from_version", 0),
        "to_version": params.get("to_version", 1),
    }


def _handle_health_check(params: dict[str, Any]) -> dict[str, Any]:
    return service.health_check()


def _handle_status(params: dict[str, Any]) -> dict[str, Any]:
    return service.status()


def _handle_overview(params: dict[str, Any]) -> list[dict[str, Any]]:
    return service.overview()


def _handle_jobs(params: dict[str, Any]) -> dict[str, Any]:
    return service.jobs()


def _handle_permissions(params: dict[str, Any]) -> dict[str, Any]:
    return service.permissions()


def _handle_exposure(params: dict[str, Any]) -> dict[str, Any]:
    return service.exposure()


def _handle_diff(params: dict[str, Any]) -> dict[str, Any]:
    return service.diff()


def _handle_manifest(params: dict[str, Any]) -> dict[str, Any]:
    return service.manifest()


def _handle_events_tail(params: dict[str, Any]) -> list[dict[str, Any]]:
    return service.events_tail(int(params.get("limit", 50)))


def _handle_logs(params: dict[str, Any]) -> dict[str, Any]:
    return service.logs(
        str(params.get("source", "")),
        str(params.get("filter", "")),
    )


def _handle_logs_text(params: dict[str, Any]) -> dict[str, Any]:
    return service.logs_text(int(params.get("limit", 80)))


def _handle_profiles_list(params: dict[str, Any]) -> list[dict[str, Any]]:
    return service.profiles_list()


def _handle_profile_get(params: dict[str, Any]) -> dict[str, Any]:
    return service.profile_get(str(params.get("name", "")))


def _handle_action_status(params: dict[str, Any]) -> dict[str, Any]:
    return service.action_status()


def _handle_stack_start(params: dict[str, Any]) -> dict[str, Any]:
    return service.stack_start(
        str(params.get("name", "")),
        bool(params.get("preview", False)),
    )


def _handle_stack_down(params: dict[str, Any]) -> dict[str, Any]:
    return service.stack_down()


def _handle_profile_use(params: dict[str, Any]) -> dict[str, Any]:
    return service.profile_use(str(params.get("name", "")))


def _handle_stop_host(params: dict[str, Any]) -> dict[str, Any]:
    return service.stop_host()


def _handle_ingress_control(params: dict[str, Any]) -> dict[str, Any]:
    return service.ingress_control(str(params.get("action", "up")))


def _handle_issue_tokens(params: dict[str, Any]) -> dict[str, Any]:
    return service.issue_tokens()


def _handle_run_doctor(params: dict[str, Any]) -> dict[str, Any]:
    return service.run_doctor()


def main() -> None:
    """Register handlers and serve the JSON-RPC loop."""
    server = RpcPluginServer()
    server.set_init_handler(_handle_init)
    server.register("_migrate_db", _handle_migrate_db)
    server.register("health_check", _handle_health_check)
    server.register("status", _handle_status)
    server.register("overview", _handle_overview)
    server.register("jobs", _handle_jobs)
    server.register("permissions", _handle_permissions)
    server.register("exposure", _handle_exposure)
    server.register("diff", _handle_diff)
    server.register("manifest", _handle_manifest)
    server.register("events_tail", _handle_events_tail)
    server.register("logs", _handle_logs)
    server.register("logs_text", _handle_logs_text)
    server.register("profiles_list", _handle_profiles_list)
    server.register("profile_get", _handle_profile_get)
    server.register("action_status", _handle_action_status)
    server.register("stack_start", _handle_stack_start)
    server.register("stack_down", _handle_stack_down)
    server.register("profile_use", _handle_profile_use)
    server.register("stop_host", _handle_stop_host)
    server.register("ingress_control", _handle_ingress_control)
    server.register("issue_tokens", _handle_issue_tokens)
    server.register("run_doctor", _handle_run_doctor)
    server.serve()


if __name__ == "__main__":
    main()
