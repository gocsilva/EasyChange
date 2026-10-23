from easychange.core.process_service import ProcessService


def test_legacy_job_log_pages_preserve_utf8_codepoints(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    service = ProcessService(tmp_path)
    service._read_job = lambda _job_id: {
        "state": "SUCCEEDED",
        "stdout": "a" * 254 + "😀" + "end",
    }

    first = service.job_logs("JLEGACY_UTF8", max_bytes=256)
    assert first["text"] == "a" * 254
    assert first["next_cursor"] == 254
    assert first["returned_bytes"] == 254

    second = service.job_logs(
        "JLEGACY_UTF8", cursor=first["next_cursor"], max_bytes=256
    )
    assert second["text"] == "😀end"
    assert second["complete"] is True
    assert second["next_cursor"] is None
