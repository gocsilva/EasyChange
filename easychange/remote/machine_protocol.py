from __future__ import annotations

import json

from easychange.core.result import Result


def encode_result(result: Result) -> str:
    """One compact JSON object per line for serial/HID bridge integrations."""
    return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))


def decode_command(payload: str) -> str:
    value = json.loads(payload)
    command = value.get("command") if isinstance(value, dict) else None
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Protocol message must contain a non-empty command string")
    return command
