from __future__ import annotations

import sys

from easychange.core.process_service import ProcessService


def _collect(service: ProcessService, output_id: str, stream: str = "stdout") -> bytes:
    cursor = 0
    chunks: list[bytes] = []
    for _ in range(128):
        page = service.run_output(
            output_id,
            stream=stream,
            cursor=cursor,
            max_bytes=65536,
        )
        chunks.append(page["text"].encode("utf-8"))
        if page["complete"]:
            assert page["next_cursor"] is None
            return b"".join(chunks)
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    raise AssertionError("run_output pagination did not terminate")


def test_synchronous_long_output_is_retained_and_fully_pageable(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    size = 2 * 1024 * 1024 + 321
    marker = "END-MARKER"
    result = service.run(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                f"sys.stdout.write('A'*{size});"
                f"sys.stdout.write({marker!r});"
                "sys.stdout.flush()"
            ),
        ],
        timeout=60,
    )

    assert result["returncode"] == 0
    assert result["output_truncated"] is True
    assert result["output_available"] is True
    assert result["output_id"]
    assert result["capture_backend"] == "tempfile-paged"
    assert len(result["stdout"].encode("utf-8")) <= 4000

    full = _collect(service, result["output_id"])
    expected = ("A" * size + marker).encode("utf-8")
    assert full == expected
    assert len(full) > 2 * 1024 * 1024


def test_retained_long_output_is_redacted_on_disk_across_stream_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    secret = "VERY-LONG-SECRET-" + ("Z" * 300)

    # Prefix length deliberately crosses the 4096-byte pump boundary.
    result = service.run(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.write('P'*4090);"
                f"sys.stdout.write({secret!r});"
                "sys.stdout.write('Q'*20000);"
                "sys.stdout.flush()"
            ),
        ],
        timeout=30,
        redact_values=[secret],
    )

    assert result["output_truncated"] is True
    assert result["output_id"]
    full = _collect(service, result["output_id"]).decode("utf-8")
    assert secret not in full
    assert "[REDACTED]" in full

    disk = (service.root / f"{result['output_id']}.out").read_bytes()
    assert secret.encode("utf-8") not in disk
    assert b"[REDACTED]" in disk


def test_durable_output_pages_do_not_split_utf8_codepoints(tmp_path, monkeypatch):
    """EC2 consumers must reconstruct Unicode output without replacement glyphs."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    output_id = "UTF8PAGE01"
    payload = ("a" * 255 + "€" + "b" * 255 + "🧪" + "c" * 300).encode("utf-8")
    (service.root / f"{output_id}.out").write_bytes(payload)
    monkeypatch.setattr(service, "_read_job", lambda _job_id: {"state": "SUCCEEDED"})

    for read_page in (service.run_output, service.job_logs):
        cursor = 0
        reconstructed = []
        for _ in range(10):
            page = read_page(output_id, cursor=cursor, max_bytes=256)
            assert "�" not in page["text"]
            assert page["returned_bytes"] == len(page["text"].encode("utf-8"))
            reconstructed.append(page["text"])
            if page["complete"]:
                break
            assert page["next_cursor"] > cursor
            cursor = page["next_cursor"]
        else:
            raise AssertionError("UTF-8 paging did not terminate")
        assert "".join(reconstructed).encode("utf-8") == payload


def test_job_log_pages_reject_hardlinks_to_foreign_files(tmp_path, monkeypatch):
    """Remote EC2 log retrieval must not expose an aliased private file."""
    import os
    import pytest

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    output_id = "JHARDLINK01"
    private = tmp_path / "private.txt"
    private.write_text("SENSITIVE_FOREIGN_CONTENT", encoding="utf-8")
    alias = service.root / f"{output_id}.out"
    try:
        os.link(private, alias)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on this filesystem: {exc}")
    monkeypatch.setattr(service, "_read_job", lambda _job_id: {"state": "SUCCEEDED"})

    page = service.job_logs(output_id)
    assert "SENSITIVE_FOREIGN_CONTENT" not in page["text"]
    with pytest.raises(LookupError):
        service.run_output(output_id)
