"""Model-free metadata for EasyChange's physical HID/HDMI MCP plugin."""
from __future__ import annotations
from pathlib import Path
from functools import lru_cache
import io
import tomllib

MAX_PROJECT_METADATA_BYTES = 131072
from easychange.core.operation_registry import (
    structured_capabilities, structured_operation_profile,
    structured_operation_is_mutation,
)

ROOT=Path(__file__).resolve().parents[1]

@lru_cache(maxsize=8)
def _project_version(path: str, modified_ns: int, size: int) -> str:
    # Polling /status should not parse TOML on every refresh. File metadata
    # invalidates the small cache for editable-development installs.
    del modified_ns, size
    with Path(path).open("rb") as handle:
        raw=handle.read(MAX_PROJECT_METADATA_BYTES+1)
    if len(raw)>MAX_PROJECT_METADATA_BYTES:
        raise ValueError("MODULE_PROJECT_METADATA_TOO_LARGE")
    project=tomllib.load(io.BytesIO(raw)).get("project",{})
    return str(project.get("version","unknown"))

def module_status(*, esp_enabled: bool = False) -> dict:
    manifest=ROOT/"pyproject.toml"
    try:
        metadata=manifest.stat()
    except FileNotFoundError:
        # Wheels/packaged services may not contain the source pyproject.
        # Their read-only module status should still be available.
        version="unknown"
    else:
        version=_project_version(str(manifest),metadata.st_mtime_ns,metadata.st_size)
    return {"installed":True,"module_root":str(ROOT),
            "version":version,
            "physical_transport":"ESP HID + HDMI EC2",
            "esp_selected":bool(esp_enabled),
            "session_ready":False,
            "requires_fresh_focus_and_image_verification":True,
            "reason":"Physical REMOTE_PC session not established by metadata inspection"}

def capabilities() -> dict:
    return structured_capabilities()

def operation_profile(operation: str, *, allow_write: bool = False,
                      allow_mutation: bool = False) -> dict:
    if len(str(operation))>100:
        raise ValueError("OPERATION_TOO_LONG")
    item={"type":str(operation),"allow_write":allow_write,
          "allow_mutation":allow_mutation}
    return {"operation":operation,
            **structured_operation_profile(operation),
            "requires_permission":structured_operation_is_mutation(item)}
