"""Durable receipt mutations must enter the contextmanager, not the method."""
from __future__ import annotations

import ast
from pathlib import Path

from easychange.core.result_store import DurableResultStore


def test_all_claim_lock_with_statements_invoke_the_contextmanager():
    source=(Path(__file__).resolve().parents[1]/
            "easychange"/"core"/"result_store.py").read_text(encoding="utf-8")
    syntax=ast.parse(source)
    uses=[]
    for node in ast.walk(syntax):
        if isinstance(node,(ast.With,ast.AsyncWith)):
            for item in node.items:
                expression=item.context_expr
                if isinstance(expression,ast.Call) and isinstance(
                        expression.func,ast.Attribute) and (
                        expression.func.attr=="_claim_lock"):
                    assert not expression.args and not expression.keywords
                    uses.append(node.lineno)
                elif isinstance(expression,ast.Attribute) and (
                        expression.attr=="_claim_lock"):
                    raise AssertionError(
                        f"Bare _claim_lock method at line {node.lineno}; "
                        "use with self._claim_lock():")
    assert len(uses)>=4


def test_claim_update_header_complete_and_replay_use_real_file_lock(tmp_path):
    store=DurableResultStore(tmp_path/"receipts")
    seq="QLOCKREG1"
    command_hash="a"*64
    command=":patch sample.txt"
    first,acquired=store.claim_running(seq,command_hash,command,mutation=True)
    assert acquired is True
    assert first["state"]=="RUNNING"
    same,second_owner=store.claim_running(seq,command_hash,command,mutation=True)
    assert second_owner is False
    assert same["execution_id"]==first["execution_id"]

    # The metadata read used to fail with TypeError on a cache miss.
    running=store.header(seq)
    assert running["sequence"]==seq
    state=store.update_mutation_state(
        seq,"APPLIED",mutation_receipt={
            "state":"COMMITTED","lifecycle_state":"APPLIED","verified":True})
    assert state["mutation_state"]=="APPLIED"
    result={"ok":True,"command":"patch","sequence":seq,
            "data":{"changed":True}}
    done=store.complete(
        seq,command_hash,command,result,
        mutation_receipt={"state":"COMMITTED","lifecycle_state":"APPLIED",
                          "verified":True})
    assert done["state"]=="DONE"
    assert done["mutation_state"]=="APPLIED"
    final=store.header(seq)
    assert final["finished"] is True
    assert final["sequence"]==seq

    reopened=DurableResultStore(tmp_path/"receipts")
    replay,owns=reopened.claim_running(seq,command_hash,command,mutation=True)
    assert owns is False
    assert replay["execution_id"]==first["execution_id"]
    assert replay["state"]=="DONE"


def test_completed_receipt_requires_prior_exclusive_sequence_claim(tmp_path):
    import pytest

    store=DurableResultStore(tmp_path/"receipts")
    sequence="QNOCLAIM1"
    digest="a"*64
    command=":write example.txt"
    completed={"ok":True,"sequence":sequence,"data":{"applied":True}}
    with pytest.raises(RuntimeError,match="RESULT_SEQUENCE_NOT_CLAIMED"):
        store.complete(sequence,digest,command,completed,
                       mutation_receipt={"state":"COMMITTED"})
    assert store.get(sequence) is None

    claimed,owns=store.claim_running(sequence,digest,command,mutation=True)
    assert owns is True
    done=store.complete(sequence,digest,command,completed,
                        mutation_receipt={"state":"COMMITTED"})
    assert done["state"]=="DONE"
    assert done["execution_id"]==claimed["execution_id"]
    duplicate=store.complete(sequence,digest,command,
                             {"ok":False,"data":{"applied":False}})
    assert duplicate==done
    with pytest.raises(ValueError,match="RESULT_SEQUENCE_COMMAND_HASH_MISMATCH"):
        store.complete(sequence,"b"*64,command,completed)


def test_duplicate_completion_avoids_large_result_serialization(tmp_path, monkeypatch):
    from unittest.mock import Mock
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path/"receipts")
    seq="QFASTREPLAY1"
    original={"ok":True,"sequence":seq,"data":{"changed":True}}
    store.claim_running(seq,"a"*64,":operation",mutation=True)
    committed=store.complete(seq,"a"*64,":operation",original)

    # Simulate a duplicate response whose result would be expensive or not
    # serializable. The previous terminal receipt must be returned before
    # any JSON encoding or hash computation of the supplied duplicate value.
    with monkeypatch.context() as context:
        context.setattr(result_module.json,"dumps",Mock(
            side_effect=AssertionError("DUPLICATE_RESULT_RESERIALIZED")))
        assert store.complete(seq,"a"*64,":operation",
                              {"ok":False,"data":{"unexpected":object()}})==committed
        import pytest
        with pytest.raises(ValueError,match="RESULT_SEQUENCE_COMMAND_HASH_MISMATCH"):
            store.complete(seq,"b"*64,":operation",original)
        with pytest.raises(RuntimeError,match="RESULT_SEQUENCE_NOT_CLAIMED"):
            store.complete("QFASTREPLAY2","a"*64,":operation",original)


def test_cross_process_terminal_race_rechecks_claim_after_json_encoding(tmp_path, monkeypatch):
    folder=tmp_path/"receipts"
    first=DurableResultStore(folder)
    second=DurableResultStore(folder)
    seq="QRACEENCODE1"
    original={"ok":True,"sequence":seq,"data":{"writer":"first"}}
    competing={"ok":True,"sequence":seq,"data":{"writer":"second"}}
    first.claim_running(seq,"a"*64,":operation",mutation=False)
    encode_result=first._encode_result_json
    injected=[]

    def during_first_encoding(value):
        if not injected and value is original:
            injected.append(True)
            second.complete(seq,"a"*64,":operation",competing)
        return encode_result(value)

    with monkeypatch.context() as context:
        context.setattr(first,"_encode_result_json",during_first_encoding)
        returned=first.complete(seq,"a"*64,":operation",original)
    assert injected==[True]
    assert returned["result"]["data"]["writer"]=="second"
    assert first.get(seq)["result"]["data"]["writer"]=="second"
    assert returned["state"]=="DONE"
