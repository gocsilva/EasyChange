from __future__ import annotations

from dataclasses import asdict, dataclass, field
from itertools import islice
from typing import Any


@dataclass(slots=True)
class Result:
    ok: bool
    command: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    code: str | None = None
    command_id: str | None = None
    duration_ms: int = 0
    sequence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_wire_dict(self) -> dict[str, Any]:
        """Shallow projection for transport/result-store hot paths."""
        return {
            "ok": self.ok,
            "command": self.command,
            "data": self.data,
            "error": self.error,
            "code": self.code,
            "command_id": self.command_id,
            "duration_ms": self.duration_ms,
            "sequence": self.sequence,
        }

    def render(self, output: str = "text") -> str:
        if output == "json":
            import json
            return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if output == "compact":
            import json
            prefix = f"EC1 {self.sequence} " if self.sequence else ""
            if not self.ok:
                essential = {"error": self.error, "code": self.code}
                if self.data:
                    essential["keys"] = [str(k)[:80] for k in islice(self.data,12)]
                details = json.dumps(essential, ensure_ascii=False, separators=(",", ":"))
                return f"{prefix}ERR {self.command_id or '-'} {self.code or 'ERROR'} {self.command} {details}"
            encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
            parts: list[str] = []
            observed_bytes = 0
            inline = True
            for part in encoder.iterencode(self.data):
                remaining=900-observed_bytes
                # The encoder can yield a megabyte-scale quoted string.
                # One Unicode character consumes at least one UTF-8 byte,
                # so stop before allocating a full encoded copy.
                if len(part)>remaining:
                    observed_bytes+=remaining+1
                    inline=False
                    break
                observed_bytes+=len(part.encode("utf-8"))
                if observed_bytes>900:
                    inline=False
                    break
                parts.append(part)
            if inline:
                details = "".join(parts)
            else:
                summary = {
                    "optical": "EC2",
                    "payload_bytes_min": observed_bytes,
                    "payload_truncated": True,
                    "keys": [str(k)[:80] for k in islice(self.data,12)],
                }
                for key in ("path", "file", "count", "state", "workspace", "line", "transaction"):
                    if key in self.data:
                        value=self.data[key]
                        if isinstance(value,str):
                            summary[key]=value[:256]+("..." if len(value)>256 else "")
                        elif value is None or type(value) in (int,float,bool):
                            summary[key]=value
                        else:
                            # Never stringify arbitrarily large nested
                            # containers on the already-truncated EC1 lane.
                            summary[key]="<"+type(value).__name__+">"
                details = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            return f"{prefix}OK {self.command_id or '-'} {self.command} {self.duration_ms}ms {details}"
        status = "OK" if self.ok else "ERROR"
        lines = [status, f"COMMAND: {self.command.upper()}"]
        if self.command_id:
            lines.append(f"ID: {self.command_id}")
        if self.code:
            lines.append(f"CODE: {self.code}")
        lines.append(f"DURATION_MS: {self.duration_ms}")
        if self.error:
            lines.append(f"MESSAGE: {self.error}")
        for key, value in self.data.items():
            if isinstance(value, list):
                lines.append(f"{key.upper()}: {len(value)}")
                lines.extend(_render_item(item) for item in value)
            else:
                lines.append(f"{key.upper()}: {value}")
        return "\n".join(lines)


def _render_item(item: Any) -> str:
    if isinstance(item, dict):
        return "|".join(str(v).replace("\n", " ") for v in item.values())
    return str(item)
