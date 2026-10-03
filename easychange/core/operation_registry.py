from __future__ import annotations

STRUCTURED_MUTATION_TYPES = frozenset({
    "write_file", "create_file", "replace_line", "replace_range", "replace_text",
    "replace_exact", "replace_anchor", "insert_before", "insert_after", "insert", "append",
})
STRUCTURED_RECOVERY_TYPES = frozenset({
    "result_status", "mutation_status", "result_header", "result_get", "result_meta", "result_chunk",
    "optical_meta", "optical_chunk", "optical_chunks",
})
STRUCTURED_JOB_TYPES = frozenset({"job_start", "job_status", "job_result", "job_logs", "job_cancel"})
TEXT_MUTATION_COMMANDS = frozenset({
    "write", "save", "force-write", "append", "insert", "replace", "replace-line",
    "replace-range", "delete", "rename", "move", "mkdir", "new", "batch",
})
STRUCTURED_CONTINUE_SAFE_TYPES = frozenset({
    "read", "search", "locate", "study", "definition", "references",
    "discover_many", "read_regions", "read_many", "runtime_profiles", "runtime_environment",
    "git_diff", "process_logs", "test_job_result", "test_job_logs", "build_job_result", "build_job_logs",
    "db_connections", "db_schema", *STRUCTURED_RECOVERY_TYPES,
})
_OPERATION_FAMILIES = {
    **{name: "mutation" for name in STRUCTURED_MUTATION_TYPES},
    **{name: "recovery" for name in STRUCTURED_RECOVERY_TYPES},
    **{name: "job" for name in STRUCTURED_JOB_TYPES},
    "read": "read", "read_many": "read", "read_regions": "read",
    "search": "discovery", "locate": "discovery", "study": "discovery",
    "definition": "discovery", "references": "discovery", "discover_many": "discovery",
    "runtime_profiles": "runtime", "runtime_configure": "runtime", "runtime_environment": "runtime",
    "run_project": "runtime", "git_diff": "git",
    "test_job_start": "job", "test_job_result": "job", "test_job_logs": "job",
    "build_job_start": "job", "build_job_result": "job", "build_job_logs": "job",
    "test_smart": "runtime", "test_evidence": "runtime", "swagger_evidence": "runtime",
    "evidence": "runtime", "process_logs": "runtime", "stop_process": "runtime",
    "db_connections": "database", "db_configure": "database", "db_schema": "database",
    "db_query": "database", "validate": "validation",
}

def structured_operation_profile(kind: str) -> dict:
    normalized = str(kind or "").casefold()
    return {
        "family": _OPERATION_FAMILIES.get(normalized, "unknown"),
        "mutation": normalized in STRUCTURED_MUTATION_TYPES,
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
