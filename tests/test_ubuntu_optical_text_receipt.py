"""Stable OCR-safe Ubuntu visual packets and bounded previews."""
import base64
import binascii
import re
from types import SimpleNamespace

from easychange.core.result import Result
from easychange.ui.main_window import MainWindow, _ubuntu_text_frame


def decode(frame):
    lines=frame.splitlines()
    hdr=lines[0].split()
    count=int(hdr[4])
    assert hdr[:2]==["ECO1","UBUNTU"]
    assert hdr[-1]=="B32"
    assert 1<=count<=24
    assert len(lines)==count+2
    pieces=[line.split()[-1] for line in lines[1:-1]]
    wire="".join(pieces)
    raw=base64.b32decode(wire+"="*(-len(wire)%8))
    assert f"{binascii.crc32(raw)&0xffffffff:08X}"==lines[-1].split()[-1]
    return raw.decode("utf8")


def test_ubuntu_small_result_retains_exact_compact_bytes():
    receipt=Result(ok=True, command="state", sequence="QABC123",
                   command_id="C123",data={"state":"READY"})
    assert decode(_ubuntu_text_frame(receipt))==receipt.render("compact")


def test_ubuntu_big_result_is_explicit_verified_preview_without_mutating_receipt():
    receipt=Result(ok=True,command="read",sequence="QLARGE123",
                   command_id="C123",data={"lines":["abcdef" * 1000]})
    original=receipt.render("compact")
    preview=decode(_ubuntu_text_frame(receipt))
    assert "payload_truncated" in preview
    assert "optical" in preview or "result_header/result_chunk" in preview
    assert original==receipt.render("compact")
    assert "abcdef" * 1000 not in preview


def test_ubuntu_failures_preserve_error_and_checksum():
    result=Result(ok=False,command="read",sequence="QERR123",
                  command_id="C123",code="NOT_FOUND",error="missing source")
    frame=_ubuntu_text_frame(result)
    assert " QERR123 ERR " in frame.splitlines()[0]
    assert "missing source" in decode(frame)


def test_ubuntu_display_separates_receipt_and_human_output_with_real_newlines():
    result=Result(ok=True,command="state",sequence="QABC123",
                  command_id="C123",data={"state":"READY"})
    captured=[]
    window=SimpleNamespace(
        _optical_audit_encode_started=0,
        _last_machine_result=None,
        _optical_timer=SimpleNamespace(stop=lambda: None),
        _optical_render_generation=0,
        _discard_optical_render_jobs=lambda: None,
        _optical_render_pending=None,
        _optical_payloads=[],
        _optical_header_payload=None,
        _optical_pixmaps=SimpleNamespace(clear=lambda: None),
        _optical_page=0,
        machine_qrs=[],
        _machine=True,
        remote_transport_mode="UBUNTU",
        _host_optical_preference="UBUNTU",
        _set_optical_burst=lambda _enabled: None,
        output=SimpleNamespace(
            setPlainText=captured.append,
            verticalScrollBar=lambda: SimpleNamespace(setValue=lambda _value: None),
        ),
    )

    MainWindow._update_machine_qr(window,result)

    assert len(captured)==1
    displayed=captured[0]
    frame=_ubuntu_text_frame(result)
    assert displayed.startswith(frame+"\n\n")
    assert "\\n" not in displayed
    assert displayed[len(frame)+2:]==result.render("text")
