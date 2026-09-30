"""Performance guardrail for the AI/HID/HDMI execution path."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def timed(service: CommandService, command: str):
    started = time.perf_counter()
    result = service.execute(command)
    return (time.perf_counter() - started) * 1000, result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="easychange-perf-") as folder:
        root = Path(folder)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        for index in range(500):
            name = "TargetService" if index == 377 else f"Service{index}"
            (root / "src" / f"{name}.cs").write_text(
                "\n".join([
                    f"namespace Bench.N{index};",
                    f"public sealed class {name} {{",
                    *[f"    public string Value{line} => \"{index}-{line}\";" for line in range(25)],
                    "}",
                ]) + "\n",
                encoding="utf-8",
            )
        (root / "tests" / "TargetServiceTests.cs").write_text(
            "public class TargetServiceTests { TargetService value = new TargetService(); }\n",
            encoding="utf-8",
        )

        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "src", "tests"], cwd=root, check=True)

        service = CommandService(Workspace.open(root))
        try:
            cold_ms, cold = timed(service, ":search TargetService")
            # Ensure the background build has a deterministic completion point
            # before warm measurements.
            service.indexer.refresh(force=True)
            warm_ms, warm = timed(service, ":search TargetService")
            study_ms, study = timed(service, ":study TargetService --limit 8 --context 3")
            paths = [f"src/Service{i}.cs" for i in range(8)]
            read_ms, read = timed(service, ":read-many " + " ".join(paths) + " --count 120")

            metrics = {
                "cold_search_ms": round(cold_ms, 2),
                "warm_search_ms": round(warm_ms, 2),
                "study_ms": round(study_ms, 2),
                "read_many_ms": round(read_ms, 2),
                "cold_matches": len(cold.data.get("matches", [])),
                "warm_matches": len(warm.data.get("matches", [])),
                "study_files": len(study.data.get("files", [])),
                "read_files": len(read.data.get("files", [])),
                "index_ready": service.indexer.ready,
            }
            print(json.dumps(metrics, indent=2))
            # Broad guardrails: catch catastrophic regressions, not machine noise.
            return int(
                not cold.ok
                or not warm.ok
                or not study.ok
                or not read.ok
                or metrics["cold_search_ms"] > 1000
                or metrics["warm_search_ms"] > 250
                or metrics["read_many_ms"] > 250
            )
        finally:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
