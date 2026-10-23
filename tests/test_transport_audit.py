"""Local-only bidirectional transport audit; no remote HDMI/ESP traffic."""
import json

from easychange.core import transport_audit as audit
from easychange.core.command_service import CommandService
from easychange.core.operation_registry import structured_operation_profile
from easychange.core.workspace import Workspace


def test_remote_transport_log_is_persistent_redacted_and_paginated(tmp_path,monkeypatch):
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    audit.log("ec1_received","QLOGSEQ11","MCP_TO_EASYCHANGE",
              mode="PIXEL",bytes=120,sha256=audit.signature("secret=VERY-SENSITIVE")["sha256"],
              payload="secret=VERY-SENSITIVE")
    audit.log("ec2_page_painted","QLOGSEQ11","EASYCHANGE_TO_MCP",
              mode="HYBRID",page=0,pages=2,packets=32,frames=24,chunks=8)
    data=audit.snapshot(sequence="QLOGSEQ11",limit=8)
    assert len(data["events"])==2
    assert data["events"][1]["frames"]==24
    assert "VERY-SENSITIVE" not in audit.FILE.read_text(encoding="utf-8")
    assert not audit.snapshot(after=data["next_cursor"],limit=4)["events"]
    assert structured_operation_profile("transport_audit")["side_effect"] is False
    assert not structured_operation_profile("transport_audit")["continue_on_error_safe"] is False


def test_remote_command_and_structured_audit_query(tmp_path,monkeypatch):
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    service=CommandService(Workspace.open(tmp_path))
    try:
        first=service.execute(":ec QAUDIT101 :state --compact")
        assert first.sequence=="QAUDIT101"
        logs=service.execute(":transport_log 0 12 QAUDIT101")
        assert logs.ok and any(x["stage"]=="ec1_received" for x in logs.data["events"])
        structured=service.execute(
            ':ec QAUDIT102 :j1 {"op":"operation","type":"transport_audit",'
            '"after":0,"limit":8,"sequence":"QAUDIT101"}')
        assert structured.ok and isinstance(structured.data.get("events"),list)
        assert not service.execute(":transport_log -4").ok
    finally:
        service.close()
