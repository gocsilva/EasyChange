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


def test_transport_log_command_dispatch_requires_exact_token(tmp_path):
    """Near misses must not invoke diagnostic log reader by prefix."""
    service=CommandService(Workspace.open(tmp_path))
    try:
        for candidate in (":transport_logger", ":transport_logeverything 0 1"):
            result=service.execute(candidate)
            assert not result.ok
            assert result.command!="transport_log"
            assert result.code != "INVALID_TRANSPORT_LOG_REQUEST"
        legitimate=service.execute(":transport_log 0 1")
        assert legitimate.ok and legitimate.command=="transport_log"
    finally:
        service.close()


def test_snapshot_never_returns_unapproved_fields_from_persisted_file(tmp_path, monkeypatch):
    """A tampered or legacy JSONL line must not exfiltrate raw EC1 credentials."""
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    audit.log("ec1_received","QPRIVATE01","MCP_TO_EASYCHANGE",bytes=5)
    with audit.FILE.open("a",encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id":audit.snapshot(limit=1)["events"][0]["id"]+5,
            "at":"2026-10-23T00:00:00Z",
            "stage":"ec1_received", "sequence":"QPRIVATE02",
            "direction":"MCP_TO_EASYCHANGE",
            "bytes":12,
            "token":"sensitive-bearer-token",
            "raw_command":":ec QPRIVATE02 password=leak",
            "result_body":{"private":"sensitive-response"},
        })+"\n")
    data=audit.snapshot(limit=10)
    assert len(data["events"])==2
    assert data["events"][-1]["bytes"]==12
    rendered=json.dumps(data)
    assert "sensitive-bearer-token" not in rendered
    assert "password=leak" not in rendered
    assert "sensitive-response" not in rendered


def test_snapshot_skips_malformed_log_records_without_crashing(tmp_path,monkeypatch):
    """Damaged/legacy JSONL cannot take down the read-only EC1 diagnostic."""
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    audit.log("ec1_received","QGOODREC1","MCP_TO_EASYCHANGE",bytes=4)
    with audit.FILE.open("a",encoding="utf-8") as handle:
        handle.write('["unexpected-json-array"]\n')
        handle.write('{"id":"not-an-integer","stage":"invalid"}\n')
        handle.write('{"id":null,"stage":"invalid"}\n')
        handle.write('{"id":-2,"stage":"invalid"}\n')
    result=audit.snapshot(after=0,limit=16)
    assert result["count"]==1
    assert result["events"][0]["sequence"]=="QGOODREC1"
