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


def test_audit_writer_rejects_nonfinite_numeric_samples(tmp_path,monkeypatch):
    """Metrics JSONL must remain standards-compliant for strict MCP parsers."""
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    audit.log("ec2_page_painted","QFINITE01","EASYCHANGE_TO_MCP",
              render_ms=float("nan"),encoding_ms=float("inf"),
              duration_ms=float("-inf"),packets=32)
    raw=audit.FILE.read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert "Infinity" not in raw
    assert "-Infinity" not in raw
    assert json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE")))["packets"]==32


def test_snapshot_validates_persisted_field_types_before_returning(tmp_path, monkeypatch):
    """Allowlisted field names cannot smuggle nested tampered payloads."""
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    audit.log("ec1_received","QSAFEVAL1","MCP_TO_EASYCHANGE",bytes=5)
    with audit.FILE.open("a",encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id":audit.snapshot(limit=1)["events"][0]["id"]+5,
            "at":"2026-10-23T00:00:00Z",
            "stage":{"token":"nested-sensitive-value"},
            "sequence":"QSAFEVAL2", "direction":"MCP_TO_EASYCHANGE",
            "bytes":{"raw":"private-payload"},
        })+"\n")
        handle.write(json.dumps({
            "id":audit.snapshot(limit=1)["events"][0]["id"]+10,
            "at":"2026-10-23T00:00:01Z",
            "stage":"ec1_received", "sequence":"QSAFEVAL3",
            "direction":"MCP_TO_EASYCHANGE",
            "bytes":{"raw":"private-payload"},
            "cause":["nested-sensitive-value"],
        })+"\n")
    result=audit.snapshot(limit=16)
    assert result["count"]==2
    assert result["events"][-1]["stage"]=="ec1_received"
    assert "bytes" not in result["events"][-1]
    assert "cause" not in result["events"][-1]
    rendered=json.dumps(result)
    assert "nested-sensitive-value" not in rendered
    assert "private-payload" not in rendered


def test_audit_event_ids_remain_monotonic_when_clock_moves_backward(tmp_path,monkeypatch):
    """Cursor pagination must not lose newer entries on clock adjustments."""
    directory=tmp_path/"audit"
    monkeypatch.setattr(audit,"ROOT",directory)
    monkeypatch.setattr(audit,"FILE",directory/"ec-audit.jsonl")
    monkeypatch.setattr(audit.time,"time_ns",iter((900000000000000000,899999999999999998)).__next__)
    audit.log("ec1_received","QCLOCK001","MCP_TO_EASYCHANGE")
    first=audit.snapshot(limit=1)["events"][0]["id"]
    audit.log("ec2_page_painted","QCLOCK001","EASYCHANGE_TO_MCP",packets=8)
    second=audit.snapshot(limit=2)["events"][-1]["id"]
    assert second > first
    paged=audit.snapshot(after=first,limit=4)
    assert paged["count"]==1
    assert paged["events"][0]["stage"]=="ec2_page_painted"
