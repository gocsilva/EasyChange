from __future__ import annotations

from types import SimpleNamespace

from easychange.core.process_service import ProcessService
from easychange.core.runtime_service import ProjectRuntimeService
from easychange.core.workspace import Workspace


LOCK_LINE = (
    'error MSB3027: Could not copy "obj\\Debug\\net8.0\\apphost.exe" to '
    '"bin\\Debug\\net8.0\\App.exe". Exceeded retry count of 10. Failed. '
    'The file is locked by: "OwnedApp (4321)"'
)


def test_build_lock_diagnostics_marks_owned_pid_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    service._processes["JOWNED"] = SimpleNamespace(
        process=SimpleNamespace(pid=4321)
    )

    diagnosis = service.diagnose_file_locks({
        "returncode": 1,
        "stderr": LOCK_LINE,
    })

    assert diagnosis["detected"] is True
    assert "MSB3027" in diagnosis["codes"]
    assert diagnosis["safe_auto_stop"] is True
    assert diagnosis["foreign_blockers"] == []
    assert diagnosis["owned_blockers"][0]["pid"] == 4321
    assert diagnosis["owned_blockers"][0]["owned_process_id"] == "JOWNED"
    assert diagnosis["owned_blockers"][0]["safe_to_stop"] is True


def test_build_lock_diagnostics_never_marks_foreign_pid_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    diagnosis = service.diagnose_file_locks({
        "returncode": 1,
        "stderr": LOCK_LINE.replace("OwnedApp (4321)", "ForeignApp (9876)"),
    })

    assert diagnosis["detected"] is True
    assert diagnosis["safe_auto_stop"] is False
    assert diagnosis["owned_blockers"] == []
    assert diagnosis["foreign_blockers"][0]["pid"] == 9876
    assert diagnosis["foreign_blockers"][0]["safe_to_stop"] is False


def test_stop_owned_blockers_skips_foreign_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    stopped_ids = []
    monkeypatch.setattr(
        service,
        "stop",
        lambda process_id: stopped_ids.append(process_id) or {"state": "STOPPED"},
    )

    result = service.stop_owned_blockers({
        "blockers": [
            {
                "pid": 4321,
                "process_name": "OwnedApp",
                "owned_by_easychange": True,
                "owned_process_id": "JOWNED",
            },
            {
                "pid": 9876,
                "process_name": "ForeignApp",
                "owned_by_easychange": False,
                "owned_process_id": None,
            },
        ]
    })

    assert stopped_ids == ["JOWNED"]
    assert result["stopped"][0]["process_id"] == "JOWNED"
    assert result["skipped"][0]["pid"] == 9876
    assert result["safe"] is False


def test_build_result_retries_only_after_owned_blocker_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)

    failed = {
        "job_id": "JBUILD1",
        "state": "FAILED",
        "terminal": True,
        "returncode": 1,
        "stdout": "",
        "stderr": LOCK_LINE,
        "diagnostics": LOCK_LINE,
        "metadata": {"job_kind": "build", "profile_id": "build-profile"},
    }
    monkeypatch.setattr(processes, "job_result", lambda job_id: dict(failed))
    monkeypatch.setattr(
        processes,
        "diagnose_file_locks",
        lambda result: {
            "detected": True,
            "codes": ["MSB3027"],
            "files": ["bin\\Debug\\net8.0\\App.exe"],
            "blockers": [{
                "pid": 4321,
                "process_name": "OwnedApp",
                "owned_by_easychange": True,
                "owned_process_id": "JOWNED",
                "safe_to_stop": True,
            }],
            "owned_blockers": [{"pid": 4321}],
            "foreign_blockers": [],
            "safe_auto_stop": True,
            "unresolved_blocker": False,
        },
    )
    monkeypatch.setattr(
        processes,
        "stop_owned_blockers",
        lambda diagnosis: {
            "stopped": [{"process_id": "JOWNED", "pid": 4321, "state": "STOPPED"}],
            "skipped": [],
            "safe": True,
        },
    )
    retries = []
    monkeypatch.setattr(
        runtime,
        "_start_runtime_job",
        lambda mode, job_id, profile_id, external_env=None: (
            retries.append((mode, job_id, profile_id, external_env))
            or {"job_id": job_id, "state": "RUNNING"}
        ),
    )

    first = runtime.build_job_result("JBUILD1", retry_owned_lock=False)
    assert first["retry_started"] is False
    assert retries == []

    retried = runtime.build_job_result("JBUILD1", retry_owned_lock=True)
    assert retried["retry_started"] is True
    assert retried["retry_job_id"] == "JBUILD1_R1"
    assert retries == [("build", "JBUILD1_R1", "build-profile", None)]


def test_build_result_never_retries_foreign_or_unresolved_blocker(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)

    monkeypatch.setattr(processes, "job_result", lambda job_id: {
        "job_id": job_id,
        "state": "FAILED",
        "terminal": True,
        "returncode": 1,
        "stderr": LOCK_LINE,
        "diagnostics": LOCK_LINE,
        "metadata": {"job_kind": "build", "profile_id": "build-profile"},
    })
    monkeypatch.setattr(processes, "diagnose_file_locks", lambda result: {
        "detected": True,
        "codes": ["MSB3027"],
        "files": [],
        "blockers": [{
            "pid": 9999,
            "process_name": "ForeignApp",
            "owned_by_easychange": False,
            "owned_process_id": None,
            "safe_to_stop": False,
        }],
        "owned_blockers": [],
        "foreign_blockers": [{"pid": 9999}],
        "safe_auto_stop": False,
        "unresolved_blocker": False,
    })
    monkeypatch.setattr(
        runtime,
        "_start_runtime_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("foreign blocker must never be auto-retried")
        ),
    )
    monkeypatch.setattr(
        processes,
        "stop_owned_blockers",
        lambda diagnosis: (_ for _ in ()).throw(
            AssertionError("foreign blocker must never be stopped")
        ),
    )

    result = runtime.build_job_result("JBUILD2", retry_owned_lock=True)
    assert result["retry_started"] is False
    assert result["retry_reason"] == "FOREIGN_OR_UNRESOLVED_BLOCKER"
