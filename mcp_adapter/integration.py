"""Model-free metadata for EasyChange's physical HID/HDMI MCP plugin."""
from __future__ import annotations
from pathlib import Path
import tomllib
from easychange.core.operation_registry import (
    structured_capabilities, structured_operation_profile,
    structured_operation_is_mutation,
)

ROOT=Path(__file__).resolve().parents[1]

def module_status(*, esp_enabled: bool = False) -> dict:
    with (ROOT/"pyproject.toml").open("rb") as handle:
        project=tomllib.load(handle).get("project",{})
    return {"installed":True,"module_root":str(ROOT),
            "version":str(project.get("version","unknown")),
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
