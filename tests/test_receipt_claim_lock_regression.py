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
