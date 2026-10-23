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

def test_corrupt_utf8_audit_line_cannot_break_snapshot_or_restart_writer(tmp_path, monkeypatch):
    """A torn/non-UTF8 JSONL entry must not take down EC1 log recovery."""
    import json
    from easychange.core import transport_audit as audit

    folder = tmp_path / "audit"
    folder.mkdir()
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")
    monkeypatch.setattr(audit, "_LAST_ID", 0)

    with audit.FILE.open("wb") as raw:
        raw.write(b'{"id":1,"at":"2026-10-23","stage":"ec1_received",'
                  b'"sequence":"QCORRUPT1","direction":"MCP_TO_EASYCHANGE",'
                  b'"payload":"\xff"}\n')
    # No remote services or HDMI calls: filesystem-only replay.
    before = audit.snapshot(limit=8)
    assert before["events"] == []
    audit.log("ec1_received", "QVALID123", "MCP_TO_EASYCHANGE", packets=1)
    after = audit.snapshot(limit=8, sequence="QVALID123")
    assert after["count"] == 1
    assert after["events"][0]["sequence"] == "QVALID123"



def test_recovery_scans_last_2000_records_without_losing_rotated_ids(tmp_path, monkeypatch):
    folder = tmp_path / "audit"
    folder.mkdir()
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")
    monkeypatch.setattr(audit, "_LAST_ID", 0)
    monkeypatch.setattr(audit.time, "time_ns", lambda: 1)

    # A high ID outside the last 2,000 rows is intentionally ignored,
    # exactly as the original deque(maxlen=2000) implementation did.
    current_rows = [json.dumps({"id": 9000000})]
    current_rows.extend(json.dumps({"id": value}) for value in range(200, 2205))
    audit.FILE.write_text("\n".join(current_rows) + "\n", encoding="utf-8")
    audit.FILE.with_suffix(".jsonl.2").write_text(
        '{"id":3000}\n{"id":3100}\n', encoding="utf-8"
    )
    audit.FILE.with_suffix(".jsonl.1").write_bytes(
        b'{"id":3200}\n{"id":3300}\n{"id":3400,"broken":"\xff"}\n'
    )

    audit.log("ec2_page_painted", "QRECOVER", "EASYCHANGE_TO_MCP", packets=2)
    last = json.loads(audit.FILE.read_text(encoding="utf-8").splitlines()[-1])
    assert last["id"] == 3301


def test_tail_reader_preserves_torn_record_and_complete_recent_rows(tmp_path):
    path = tmp_path / "audit.jsonl"
    # Cross a 64-KiB block boundary; the last partial line must not hide
    # earlier complete records during restart recovery.
    values = [json.dumps({"id": n, "padding": "A" * 70})
              for n in range(2500)]
    path.write_bytes(("\n".join(values) + "\n").encode("utf-8")
                     + b'{"id":9999,"bad":"\xff"}')
    recent = audit._recent_log_lines(path)
    assert len(recent) == 2000
    assert json.loads(recent[-2])["id"] == 2499
    assert recent[-1].endswith(b'\xff"}')



def test_snapshot_reads_newest_rotation_first_preserving_event_order(tmp_path, monkeypatch):
    folder = tmp_path / "audit"
    folder.mkdir()
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")

    def row(identifier):
        return json.dumps({
            "id": identifier, "at": "2026-10-23", "stage": "ec2_page_painted",
            "sequence": "QORDER", "direction": "EASYCHANGE_TO_MCP",
        }) + "\n"

    audit.FILE.with_suffix(".jsonl.2").write_text(
        row(1) + row(2), encoding="utf-8"
    )
    audit.FILE.with_suffix(".jsonl.1").write_text(
        row(3) + row(4), encoding="utf-8"
    )
    audit.FILE.write_text(row(5) + row(6), encoding="utf-8")
    assert [entry["id"] for entry in audit.snapshot(limit=16)["events"]] == list(range(1, 7))
    following = audit.snapshot(after=2, limit=2)
    assert [entry["id"] for entry in following["events"]] == [3, 4]
    assert following["more"] is True

    # The newest 2,000 physical rows completely cover the bounded snapshot.
    audit.FILE.write_text("".join(row(number) for number in range(10, 2020)),
                          encoding="utf-8")
    result = audit.snapshot(limit=16)
    assert [entry["id"] for entry in result["events"]] == list(range(2004, 2020))



def test_snapshot_avoids_parsing_unneeded_events(tmp_path, monkeypatch):
    folder = tmp_path / "audit"
    folder.mkdir()
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")
    with audit.FILE.open("w", encoding="utf-8") as out:
        for identifier in range(1, 2001):
            out.write(json.dumps({
                "id": identifier, "at": "2026-10-23", "stage": "ec2_page_painted",
                "sequence": "QLAST", "direction": "EASYCHANGE_TO_MCP",
            }) + "\n")

    original = audit.json.loads
    decoded = []

    def tracked_loads(*args, **kwargs):
        decoded.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit.json, "loads", tracked_loads)
    last = audit.snapshot(limit=8)
    assert [item["id"] for item in last["events"]] == list(range(1993, 2001))
    assert len(decoded) == 8

    decoded.clear()
    cursor = audit.snapshot(after=5, limit=2)
    assert [item["id"] for item in cursor["events"]] == [6, 7]
    assert cursor["more"] is True
    assert len(decoded) == 8  # five skips + two results + one lookahead



def test_audit_writer_never_persists_secret_bearing_allowed_text_fields(tmp_path, monkeypatch):
    folder = tmp_path / "audit"
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")
    monkeypatch.setattr(audit, "_LAST_ID", 0)
    marker = "VERY_SENSITIVE_AUDIT_MARKER"
    audit.log(
        "Bearer " + marker, "QVALID123", "MCP_TO_EASYCHANGE",
        command_type="authorization=" + marker,
        code="password=" + marker, mode="PIXEL+QR",
    )
    raw = audit.FILE.read_text(encoding="utf-8")
    assert marker not in raw
    assert audit.snapshot(limit=1)["events"][0]["mode"] == "PIXEL+QR"


def test_audit_snapshot_never_exposes_secrets_inside_allowed_text_fields(tmp_path, monkeypatch):
    folder = tmp_path / "audit"
    folder.mkdir()
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")
    marker = "VERY_SENSITIVE_AUDIT_MARKER"
    audit.FILE.write_text(json.dumps({
        "id": 123, "at": "2026-10-23T00:00:00Z",
        "stage": "ec1_received", "sequence": "QVALID123",
        "direction": "MCP_TO_EASYCHANGE", "mode": "PIXEL+QR",
        "command_type": "authorization=" + marker,
        "code": "password=" + marker,
    }) + "\n", encoding="utf-8")
    snapshot = audit.snapshot(limit=1)
    assert snapshot["count"] == 1
    assert snapshot["events"][0]["mode"] == "PIXEL+QR"
    assert marker not in json.dumps(snapshot)



def test_audit_snapshot_rejects_secret_bearing_timestamp(tmp_path, monkeypatch):
    folder = tmp_path / "audit"
    folder.mkdir()
    monkeypatch.setattr(audit, "ROOT", folder)
    monkeypatch.setattr(audit, "FILE", folder / "transport_audit.jsonl")
    marker = "VERY_SENSITIVE_AUDIT_MARKER"
    audit.FILE.write_text(json.dumps({
        "id": 124, "at": "Bearer " + marker,
        "stage": "ec1_received", "sequence": "QVALID123",
        "direction": "MCP_TO_EASYCHANGE",
    }) + "\n", encoding="utf-8")
    result = audit.snapshot(limit=1)
    assert result["events"] == []
    assert marker not in json.dumps(result)
