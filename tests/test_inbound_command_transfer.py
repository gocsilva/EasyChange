from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib

import pytest
from PySide6.QtWidgets import QApplication

from easychange.core.command_service import CommandService
from easychange.core.inbound_transfer import InboundCommandStore
from easychange.core.result import Result
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def _package(command: str, transfer_id: str = "U12345678ABC") -> tuple[str, list[str]]:
    raw = command.encode("utf-8")
    compressed = zlib.compress(raw, level=6)
    chunks = [
        compressed[index:index + 5120]
        for index in range(0, len(compressed), 5120)
    ]
    begin = (
        f":u1 begin {transfer_id} {len(raw)} {len(compressed)} "
        f"{len(chunks)} {hashlib.sha256(raw).hexdigest()}"
    )
    lines = []
    for index, chunk in enumerate(chunks):
        token = base64.urlsafe_b64encode(chunk).decode("ascii").rstrip("=")
        crc = f"{binascii.crc32(chunk) & 0xffffffff:08x}"
        lines.append(f":u1 chunk {transfer_id} {index} {crc} {token}")
    return begin, lines


def test_qlineedit_defensive_limit_exceeds_previous_qt_default(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        Workspace.open(tmp_path),
        machine_mode=False,
        hid_mode=False,
    )
    try:
        assert window.command.maxLength() >= 4 * 1024 * 1024
        value = "A" * 100_000
        window.command.setText(value)
        assert window.command.text() == value
    finally:
        window.close()
        app.processEvents()


def test_segmented_inbound_upload_executes_large_low_compressibility_json(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        # Hex digests are intentionally much less compressible than repeated Xs;
        # this reproduces the kind of real prompt that defeated the old tests.
        content = "\n".join(
            hashlib.sha256(f"line-{index}".encode()).hexdigest()
            for index in range(900)
        )
        payload = {
            "op": "operation",
            "type": "write_file",
            "path": "large.md",
            "content": content,
        }
        command = ":j1 " + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert len(command) > 32767

        begin, chunks = _package(command)
        assert service.execute(begin).ok is True
        for line in chunks:
            result = service.execute(line)
            assert result.ok is True
            assert result.data["suppress_optical_broadcast"] is True

        status = service.execute(":ec QSTATUS1234 :u1 status U12345678ABC")
        assert status.ok is True
        assert status.data["ready"] is True
        assert status.data["verified"] is True

        result = service.execute(":ec QCOMMIT1234 :u1 commit U12345678ABC")
        assert result.ok is True
        assert (tmp_path / "large.md").read_text(encoding="utf-8") == content
        assert result.data["inbound_upload"]["verified"] is True
    finally:
        service.close()


def test_inbound_status_reports_only_missing_chunks(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        content = "".join(
            hashlib.sha256(f"item-{index}".encode()).hexdigest()
            for index in range(500)
        )
        command = ":j1 " + json.dumps(
            {
                "op": "operation",
                "type": "write_file",
                "path": "missing.txt",
                "content": content,
            },
            separators=(",", ":"),
        )
        begin, chunks = _package(command, "UMISSING123")
        assert len(chunks) >= 2
        assert service.execute(begin).ok is True
        for index, line in enumerate(chunks):
            if index != 1:
                assert service.execute(line).ok is True

        status = service.execute(":ec QSTATUS5678 :u1 status UMISSING123")
        assert status.ok is True
        assert status.data["ready"] is False
        assert status.data["missing_indexes"] == [1]

        commit = service.execute(":ec QCOMMIT5678 :u1 commit UMISSING123")
        assert commit.ok is False
        assert commit.code == "INBOUND_TRANSFER_INCOMPLETE"
        assert commit.data["missing_indexes"] == [1]
    finally:
        service.close()


def test_inbound_chunk_duplicate_is_idempotent_and_conflict_is_rejected(tmp_path):
    store = InboundCommandStore(tmp_path / ".easychange" / "inbound")
    raw = b":state"
    compressed = zlib.compress(raw)
    transfer_id = "UDUPLICATE1"
    store.begin(
        transfer_id,
        raw_bytes=len(raw),
        compressed_bytes=len(compressed),
        chunk_count=1,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    crc = f"{binascii.crc32(compressed) & 0xffffffff:08x}"
    first = store.put_chunk(transfer_id, index=0, crc32=crc, token=token)
    second = store.put_chunk(transfer_id, index=0, crc32=crc, token=token)
    assert first["duplicate"] is False
    assert second["duplicate"] is True

    other = b"not-the-same"
    other_token = base64.urlsafe_b64encode(other).decode("ascii").rstrip("=")
    other_crc = f"{binascii.crc32(other) & 0xffffffff:08x}"
    with pytest.raises(Exception, match="INBOUND_CHUNK_CONFLICT"):
        store.put_chunk(
            transfer_id,
            index=0,
            crc32=other_crc,
            token=other_token,
        )


def test_machine_qr_suppression_skips_encoder_for_upload_control(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        Workspace.open(tmp_path),
        machine_mode=True,
        hid_mode=False,
    )
    calls = []
    monkeypatch.setattr(
        window._optical_encode_executor,
        "submit",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    try:
        window._update_machine_qr(
            Result(
                True,
                "u1",
                data={"suppress_optical_broadcast": True},
            )
        )
        assert calls == []
        assert window._optical_payloads == []
    finally:
        window.close()
        app.processEvents()
