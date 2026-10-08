from __future__ import annotations

STRUCTURED_MUTATION_TYPES = frozenset({
    "write_file", "create_file", "replace_line", "replace_range", "replace_text",
    "replace_exact", "replace_anchor", "insert_before", "insert_after", "insert", "append",
})
STRUCTURED_RECOVERY_TYPES = frozenset({
    "result_status", "mutation_status", "result_header", "result_get", "result_meta",
    "result_chunk", "result_chunks",
    "result_stream_meta", "result_stream_chunk", "result_stream_chunks",
    "optical_meta", "optical_chunk", "optical_chunks",
})
STRUCTURED_JOB_TYPES = frozenset({"job_start", "job_status", "job_result", "job_logs", "job_cancel"})
STRUCTURED_UBUNTU_TYPES = frozenset({
    "ubuntu_open", "ubuntu_state", "ubuntu_close", "ubuntu_cd", "ubuntu_env",
    "ubuntu_exec", "ubuntu_shell", "ubuntu_batch",
    "ubuntu_job_status", "ubuntu_job_logs", "ubuntu_job_cancel",
    "k8s_context", "k8s_get", "k8s_describe", "k8s_logs", "k8s_events", "k8s_command",
})
STRUCTURED_ALWAYS_SIDE_EFFECT_TYPES = frozenset({
    *STRUCTURED_MUTATION_TYPES,
    "runtime_configure", "run_project", "process_exec", "stop_process",
    "job_start", "job_cancel", "test_job_start", "build_job_start",
    "browser_open", "browser_attach", "browser_detach", "browser_actions",
    "browser_show", "browser_hide", "browser_navigate", "browser_evaluate",
    "browser_cdp_call", "browser_cdp_batch", "swagger_browser_execute",
    "db_configure", "ef_database_update", "secret_broker_import", "secret_broker_import_source",
    "ubuntu_open", "ubuntu_close", "ubuntu_cd", "ubuntu_env",
    "ubuntu_shell", "ubuntu_job_cancel",
    "optical_transport_configure",
})
STRUCTURED_CONDITIONAL_SIDE_EFFECT_TYPES = frozenset({
    "db_query", "ubuntu_exec", "ubuntu_batch", "k8s_command",
})
TEXT_MUTATION_COMMANDS = frozenset({
    "write", "save", "force-write", "append", "insert", "replace", "replace-line",
    "replace-range", "delete", "rename", "move", "mkdir", "new", "batch",
})
STRUCTURED_CONTINUE_SAFE_TYPES = frozenset({
    "read", "search", "locate", "study", "definition", "references",
    "discover_many", "read_regions", "read_many", "human_text_info", "human_text_read", "command_schema",
    "secret_broker_public", "secret_broker_status", "optical_transport_get",
    "runtime_profiles", "runtime_environment", "runtime_environment_status",
    "browser_state", "browser_snapshot", "browser_dom", "browser_visual", "browser_tabs",
    "browser_cdp_events", "swagger_browser_operations",
    "ubuntu_state", "ubuntu_job_status", "ubuntu_job_logs",
    "k8s_context", "k8s_get", "k8s_describe", "k8s_logs", "k8s_events",
    "git_diff", "process_logs", "test_job_result", "test_job_logs", "build_job_result", "build_job_logs",
    "ef_migrations_list", "ef_script",
    "db_connections", "db_schema", *STRUCTURED_RECOVERY_TYPES,
})
_OPERATION_FAMILIES = {
    **{name: "mutation" for name in STRUCTURED_MUTATION_TYPES},
    **{name: "recovery" for name in STRUCTURED_RECOVERY_TYPES},
    **{name: "job" for name in STRUCTURED_JOB_TYPES},
    "read": "read", "read_many": "read", "read_regions": "read",
    "human_text_info": "read", "human_text_read": "read",
    "search": "discovery", "locate": "discovery", "study": "discovery",
    "definition": "discovery", "references": "discovery", "discover_many": "discovery",
    "command_schema": "metadata",
    "optical_transport_get": "transport", "optical_transport_configure": "transport",
    "secret_broker_public": "secret", "secret_broker_status": "secret",
    "secret_broker_import": "secret", "secret_broker_import_source": "secret",
    "runtime_profiles": "runtime", "runtime_configure": "runtime", "runtime_environment": "runtime", "runtime_environment_status": "runtime",
    "run_project": "runtime", "process_exec": "runtime", "git_diff": "git",
    "browser_open": "browser", "browser_attach": "browser", "browser_detach": "browser",
    "browser_state": "browser", "browser_snapshot": "browser", "browser_dom": "browser",
    "browser_visual": "browser", "browser_actions": "browser", "browser_tabs": "browser",
    "browser_show": "browser", "browser_hide": "browser", "browser_navigate": "browser",
    "browser_evaluate": "browser", "browser_cdp_call": "browser",
    "browser_cdp_batch": "browser", "browser_cdp_events": "browser",
    "swagger_browser_operations": "browser", "swagger_browser_execute": "browser",
    "test_job_start": "job", "test_job_result": "job", "test_job_logs": "job",
    "build_job_start": "job", "build_job_result": "job", "build_job_logs": "job",
    "test_smart": "runtime", "test_evidence": "runtime", "swagger_evidence": "runtime",
    "evidence": "runtime", "process_logs": "runtime", "stop_process": "runtime",
    "db_connections": "database", "db_configure": "database", "db_schema": "database",
    "db_query": "database",
    "ef_migrations_list": "database", "ef_database_update": "database", "ef_script": "database",
    **{name: "ubuntu" for name in STRUCTURED_UBUNTU_TYPES if name.startswith("ubuntu_")},
    **{name: "kubernetes" for name in STRUCTURED_UBUNTU_TYPES if name.startswith("k8s_")},
    "validate": "validation",
}

def structured_operation_is_mutation(item: dict) -> bool:
    """Classify any sequenced operation that can produce an external side effect.

    This is intentionally broader than STRUCTURED_MUTATION_TYPES, which remains
    the file-edit transaction set. Durable sequence receipts use this predicate
    so process/browser/database/Kubernetes side effects get the same fail-closed
    replay semantics as file mutations.
    """
    if not isinstance(item, dict):
        return False
    kind = str(item.get("type") or "").casefold()
    if kind in STRUCTURED_ALWAYS_SIDE_EFFECT_TYPES:
        return True
    if kind == "db_query":
        return bool(item.get("allow_write", False))
    if kind in {"ubuntu_exec", "k8s_command"}:
        return bool(item.get("allow_mutation", False))
    if kind == "ubuntu_batch":
        commands = item.get("commands")
        if not isinstance(commands, list):
            return False
        return any(
            isinstance(command, dict)
            and (
                bool(command.get("allow_mutation", False))
                or bool(str(command.get("command") or "").strip())
            )
            for command in commands
        )
    return False


def structured_operation_profile(kind: str) -> dict:
    normalized = str(kind or "").casefold()
    return {
        "family": _OPERATION_FAMILIES.get(normalized, "unknown"),
        "mutation": normalized in STRUCTURED_MUTATION_TYPES,
        "side_effect": normalized in STRUCTURED_ALWAYS_SIDE_EFFECT_TYPES,
        "conditional_side_effect": normalized in STRUCTURED_CONDITIONAL_SIDE_EFFECT_TYPES,
        "recovery": normalized in STRUCTURED_RECOVERY_TYPES,
        "job": normalized in STRUCTURED_JOB_TYPES,
        "continue_on_error_safe": normalized in STRUCTURED_CONTINUE_SAFE_TYPES,
    }

def structured_capabilities() -> dict:
    operations = {name: structured_operation_profile(name) for name in sorted(_OPERATION_FAMILIES)}
    families: dict[str, list[str]] = {}
    for name, profile in operations.items():
        families.setdefault(str(profile["family"]), []).append(name)
    return {
        "max_operations": 64,
        "operations": operations,
        "families": families,
        "continue_on_error": True,
        "continue_on_error_requires_safe_read_batch": True,
        "text_mutation_commands": sorted(TEXT_MUTATION_COMMANDS),
    }
