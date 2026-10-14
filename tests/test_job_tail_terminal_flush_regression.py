from easychange.core.process_service import ProcessService


def test_terminal_tail_reads_after_finalization(monkeypatch):
    service = ProcessService.__new__(ProcessService)
    events = []

    def status(job_id):
        events.append("finalized")
        return {"state": "SUCCEEDED", "terminal": True, "pid": 7}

    def logs(job_id, **kwargs):
        events.append("logs")
        return {
            "text": "last line\n" if "finalized" in events else "",
            "terminal": False,
            "complete": True,
            "total_bytes": 10,
        }

    monkeypatch.setattr(service, "job_status", status)
    monkeypatch.setattr(service, "job_logs", logs)
    page = service.job_tail("TEST1")
    assert page["terminal"] is True
    assert page["text"] == "last line\n"
    assert events == ["finalized", "logs"]


def test_running_tail_preserves_cursor(monkeypatch):
    service = ProcessService.__new__(ProcessService)
    calls = []

    def status(job_id):
        calls.append("status")
        return {"state": "RUNNING", "terminal": False, "pid": 11}

    def logs(job_id, **kwargs):
        calls.append("logs")
        return {
            "text": "chunk",
            "cursor": kwargs["cursor"],
            "next_cursor": 9,
            "terminal": False,
        }

    monkeypatch.setattr(service, "job_status", status)
    monkeypatch.setattr(service, "job_logs", logs)
    page = service.job_tail("TEST2", cursor=4)
    assert page["cursor"] == 4 and page["next_cursor"] == 9
    assert page["terminal"] is False and page["state"] == "RUNNING"
    assert calls == ["status", "logs"]
