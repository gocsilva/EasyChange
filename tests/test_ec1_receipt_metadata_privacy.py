"""Durable EC1 receipt metadata must not persist raw command credentials."""
import hashlib
import json

from easychange.core.result_store import DurableResultStore
from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def test_durable_receipt_claim_and_completion_never_store_plaintext_command(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    secret = "SUPER_PRIVATE_COMMAND_CREDENTIAL_24"
    command = ':j1 {"op":"health","token":"' + secret + '"}'
    digest = hashlib.sha256(command.encode()).hexdigest()
    receipt, claimed = store.claim_running("QSECURE123", digest, command, mutation=False)
    assert claimed
    assert secret not in json.dumps(receipt)
    done = store.complete("QSECURE123", digest, command, {"ok": True, "command": "health", "data": {}})
    assert secret not in json.dumps(done)
    disk = b"".join(p.read_bytes() for p in (tmp_path / "results").rglob("*") if p.is_file())
    assert secret.encode() not in disk


def test_sequenced_journal_never_persists_plaintext_structured_payload(tmp_path):
    secret = "SUPER_PRIVATE_COMMAND_CREDENTIAL_24"
    service = CommandService(Workspace(tmp_path, tmp_path.name), defer_initial_maintenance=True)
    try:
        service.execute(':ec QSECURE456 :j1 {"op":"unknown","token":"' + secret + '"}')
        journal = service.state_store.data.get("ec_results", {})
        assert secret not in json.dumps(journal)
        assert secret not in (tmp_path / ".easychange" / "session.json").read_text(encoding="utf-8")
    finally:
        service.close()
