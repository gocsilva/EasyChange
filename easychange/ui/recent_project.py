"""Remember the last successfully opened EasyChange GUI workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path

STATE_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "EasyChange" / "last_project.json"

def remember_project(path: str | Path) -> None:
    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_dir():
        raise ValueError("PROJECT_NOT_DIRECTORY")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps({"workspace": str(target)}), encoding="utf-8")
        os.replace(temp, STATE_FILE)
    finally:
        temp.unlink(missing_ok=True)

def last_project() -> str | None:
    try:
        if STATE_FILE.stat().st_size > 8192:
            return None
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        raw = value.get("workspace") if isinstance(value, dict) else None
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            return None
        target = Path(raw).expanduser().resolve(strict=True)
        return str(target) if target.is_dir() else None
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
