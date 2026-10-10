"""Compact EC1 responses must stay small even with large result metadata."""
from __future__ import annotations
import json
from easychange.core.result import Result


def test_large_path_and_nested_transaction_are_summarized_without_huge_ec1_frame():
    huge="x"*300_000
    value=Result(
        True,"read",
        data={"path":huge,"transaction":{"log":huge},"count":12,
              "items":[huge]},
        sequence="QCOMPACT1",duration_ms=3)
    compact=value.render("compact")
    assert compact.startswith("EC1 QCOMPACT1 OK")
    assert len(compact)<1300
    summary=json.loads(compact.split("ms ",1)[1])
    assert summary["optical"]=="EC2"
    assert summary["payload_truncated"] is True
    assert summary["payload_bytes_min"]>900
    assert summary["path"]==huge[:256]+"..."
    assert summary["transaction"]=="<dict>"
    assert summary["count"]==12


def test_oversized_json_mapping_key_is_not_copied_into_compact_summary():
    giant_key="K"*250_000
    compact=Result(True,"search",{giant_key:1},sequence="QKEYLIMIT1").render("compact")
    assert len(compact)<600
    parsed=json.loads(compact.split("ms ",1)[1])
    assert parsed["keys"]==["K"*80]


def test_small_compact_json_retains_exact_types_and_values():
    value=Result(True,"search",{"count":2,"matches":[{"line":9}]},
                 sequence="QSHORT001")
    compact=value.render("compact")
    assert json.loads(compact.split("ms ",1)[1])=={
        "count":2,"matches":[{"line":9}]}


def test_failed_result_key_list_is_bounded_without_full_key_enumeration():
    data={str(i):i for i in range(5000)}
    compact=Result(False,"read",data,error="invalid",code="READ_ERROR").render("compact")
    details=json.loads(compact.split(" READ_ERROR read ",1)[1])
    assert len(details["keys"])==12
    assert len(compact)<500
