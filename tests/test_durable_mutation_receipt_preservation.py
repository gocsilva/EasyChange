"""Terminal completion must never erase earlier mutation audit evidence."""
from __future__ import annotations

from easychange.core.result_store import DurableResultStore


def test_terminal_completion_retains_persisted_mutation_receipt(tmp_path):
    store=DurableResultStore(tmp_path/"receipts")
    sequence="QPROOFRETAIN1"
    digest="a"*64
    command=":patch existing.txt"
    proof={"state":"APPLIED","lifecycle_state":"APPLIED",
           "mutation_id":"M123","verified":True,
           "evidence":{"before_sha256":"a"*64,"after_sha256":"b"*64}}
    store.mark_running(sequence,digest,command,mutation=True)
    store.update_mutation_receipt(sequence,proof)
    assert store.get(sequence)["mutation_receipt"]==proof

    result={"ok":True,"command":"patch","data":{"affected":1}}
    completed=store.complete(sequence,digest,command,result)
    assert completed["state"]=="DONE"
    assert completed["mutation_state"]=="APPLIED"
    assert completed["mutation_receipt"]==proof
    assert store.get(sequence)["mutation_receipt"]==proof
    assert store.header(sequence)["mutation_receipt"]==proof
    # Reopening emulates the next EasyChange process or a crash/restart.
    reopened=DurableResultStore(tmp_path/"receipts")
    assert reopened.get(sequence)["mutation_receipt"]==proof
    assert reopened.header(sequence)["mutation_receipt"]==proof


def test_explicit_receipt_overrides_old_one_but_duplicate_completion_does_not(tmp_path):
    store=DurableResultStore(tmp_path/"receipts")
    sequence="QPROOFRETAIN2"
    digest="b"*64
    command=":patch original.txt"
    old={"state":"STARTED","mutation_id":"M124"}
    final={"state":"VALIDATED","lifecycle_state":"VALIDATED",
           "mutation_id":"M124","verified":True}
    store.mark_running(sequence,digest,command,mutation=True)
    store.update_mutation_receipt(sequence,old)
    done=store.complete(sequence,digest,command,{"ok":True},
                        mutation_receipt=final)
    assert done["mutation_receipt"]==final
    again=store.complete(sequence,digest,command,{"ok":False},
                         mutation_receipt={"state":"FAILED"})
    assert again["mutation_receipt"]==final
    assert again["success"] is True
    assert store.get(sequence)["mutation_receipt"]==final
