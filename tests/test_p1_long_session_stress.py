from __future__ import annotations

import gc
import json
import os
import sys
import time
import tracemalloc

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _wait_job(fetch, timeout=10.0):
    deadline = time.time() + timeout
    result = fetch()
    while not result.get("terminal") and time.time() < deadline:
        time.sleep(0.02)
        result = fetch()
    return result


def test_long_session_stays_bounded_after_required_operation_mix(tmp_path, monkeypatch):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))

    for index in range(12):
        (tmp_path / f"code-{index}.py").write_text(
            "\n".join(
                [
                    f"class StressType{index}:",
                    "    def Needle(self):",
                    f"        return 'needle-{index}'",
                ]
                + [f"# filler {line} needle" for line in range(40)]
            )
            + "\n",
            encoding="utf-8",
        )
    mutable = tmp_path / "mutable.txt"
    mutable.write_text("TOKEN_A\n", encoding="utf-8")

    service = CommandService(Workspace.open(tmp_path))
    service.runtime.configure_profile(
        "stress",
        build_argv=[sys.executable, "-c", "print('BUILD OK')"],
        test_argv=[sys.executable, "-c", "print('5 passed, 1 skipped')"],
    )

    process = psutil.Process(os.getpid())
    gc.collect()
    rss_before = process.memory_info().rss
    tracemalloc.start()
    try:
        # I. 1000 continuous small operations.
        for _ in range(1000):
            result = service.execute(":state")
            assert result.ok is True

        # 100 studies.
        for _ in range(100):
            result = service.execute(":study Needle --limit 4 --context 1")
            assert result.ok is True

        # 50 read_many calls.
        read_args = [
            "read-many",
            *[f"code-{index}.py" for index in range(6)],
            "--count",
            "32",
            "--max-bytes",
            "65536",
        ]
        for _ in range(50):
            result = service.execute_tokens(
                "read-many",
                read_args[1:],
                raw=":read-many stress",
                persist=False,
            )
            assert result.ok is True

        # 50 deterministic mutations, alternating exact expected content.
        current = "TOKEN_A"
        for index in range(50):
            replacement = "TOKEN_B" if current == "TOKEN_A" else "TOKEN_A"
            result = service._execute_structured({
                "op": "operation",
                "type": "replace_exact",
                "path": "mutable.txt",
                "old_text": current,
                "new_text": replacement,
                "expected_occurrences": 1,
            })
            assert result.ok is True
            current = replacement

        # 20 real child-process builds/tests (10 + 10).
        for index in range(10):
            build_id = f"JSTRESSB{index:02d}"
            service.runtime.start_build_job(build_id, "stress")
            build = _wait_job(lambda job_id=build_id: service.runtime.build_job_result(job_id))
            assert build["finished"] is True and build["passed"] is True

            test_id = f"JSTRESST{index:02d}"
            service.runtime.start_test_job(test_id, "stress")
            test = _wait_job(lambda job_id=test_id: service.runtime.test_job_result(job_id))
            assert test["finished"] is True and test["passed"] is True

        maintenance = service._run_maintenance(force=True)
        gc.collect()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        rss_after = process.memory_info().rss

        assert len(service.history) <= 100
        assert len(service.journal) <= 64
        assert len(service._ec_result_cache) <= 8
        assert sum(service._ec_result_cache_sizes.values()) <= 8 * 1024 * 1024
        assert len(service.results) <= 512
        assert maintenance["process_memory"]["finished_retained"] <= 8

        # Generous cross-platform leak guard: this test is intended to catch
        # unbounded retention, not allocator noise.
        assert rss_after - rss_before < 128 * 1024 * 1024
        assert current_bytes < 96 * 1024 * 1024
        assert peak_bytes < 160 * 1024 * 1024
        print(json.dumps({
            "operations_small": 1000,
            "studies": 100,
            "read_many": 50,
            "mutations": 50,
            "build_jobs": 10,
            "test_jobs": 10,
            "rss_before_mb": round(rss_before / 1024 / 1024, 2),
            "rss_after_mb": round(rss_after / 1024 / 1024, 2),
            "rss_growth_mb": round((rss_after - rss_before) / 1024 / 1024, 2),
            "tracemalloc_current_mb": round(current_bytes / 1024 / 1024, 2),
            "tracemalloc_peak_mb": round(peak_bytes / 1024 / 1024, 2),
            "history_items": len(service.history),
            "journal_recent_metadata": len(service.journal),
            "ec_cache_items": len(service._ec_result_cache),
            "ec_cache_bytes": sum(service._ec_result_cache_sizes.values()),
            "navigation_results": len(service.results),
            "finished_processes_retained": maintenance["process_memory"]["finished_retained"],
        }, sort_keys=True))
    finally:
        tracemalloc.stop()
        service.close()
