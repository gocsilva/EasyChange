"""Software-only regression: queued and running jobs must not report terminal logs."""
from pathlib import Path

import pytest

from easychange.core.process_service import ProcessService


@pytest.mark.parametrize(
    "state,expected_terminal",
    [
        ("QUEUED", False),
        ("RUNNING", False),
        ("SUCCEEDED", True),
        ("FAILED", True),
        ("CANCELLED", True),
        ("ORPHANED", True),
    ],
)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("durable_file", [False, True])
def test_job_logs_terminal_matches_state(
    tmp_path: Path, monkeypatch, state: str, expected_terminal: bool,
    stream: str, durable_file: bool,
):
    service = ProcessService.__new__(ProcessService)
    service.root = tmp_path
    service._processes = {}
    monkeypatch.setattr(
        service, "_read_job",
        lambda job_id: {"job_id": job_id, "state": state, stream: "line"},
    )
    if durable_file:
        suffix = "out" if stream == "stdout" else "err"
        (tmp_path / f"JOB1.{suffix}").write_bytes(b"line")

    page = service.job_logs("JOB1", stream=stream, max_bytes=4096)

    assert page["terminal"] is expected_terminal
    assert page["durable_file"] is durable_file
    assert page["text"] == "line"
