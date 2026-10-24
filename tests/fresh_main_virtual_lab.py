"""Run the local virtual EasyChange lab ONLY from current remote main commits.

Each invocation fetches all canonical origins, including the BMO dependency,, checks each fetched SHA
against ls-remote, materializes disposable detached Git worktrees, runs
the lab from the pinned EasyChange tree against the pinned private MCP tree,
records revision manifest, then removes only worktrees created in this run.

No forced pull/reset in the real local user trees. Never touches hardware.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


REPOSITORIES = {
    "EasyChange": ("EasyChange", "gocsilva/EasyChange"),
    "MCP": ("MCP_PC_CONTROLLER", "gocsilva/MCP_PC_CONTROLLER"),
    "ESP": ("ESP32-PC-CONTROLLER", "gocsilva/ESP32-PC-CONTROLLER"),
    # Current MCP main imports this sibling module at package-import time.
    "BMO_LLM_CONTROLLER": ("BMO_LLM_CONTROLLER", "gocsilva/BMO_LLM_CONTROLLER"),
}
GIT_TIMEOUT = 80


def git(directory: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False, shell=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"GIT_FAILURE {directory.name} {' '.join(args[:2])}: "
            f"{result.stderr.strip()[-700:]}"
        )
    return result.stdout.strip()


def remote_main(repository: Path, expected_origin: str) -> str:
    if not repository.is_dir():
        raise RuntimeError(f"CANONICAL_REPOSITORY_MISSING: {repository}")
    origin = git(repository, "remote", "get-url", "origin")
    # Never fetch a replacement, untrusted git repository with this launcher.
    normalized = origin.lower().rstrip("/").removesuffix(".git")
    if not normalized.endswith("/" + expected_origin.lower()):
        raise RuntimeError(
            f"UNEXPECTED_REMOTE_ORIGIN: {repository.name} (expected {expected_origin})"
        )
    for attempt in (1, 2):
        git(
            repository, "fetch", "--no-tags", "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        local = git(repository, "rev-parse", "--verify", "refs/remotes/origin/main")
        remote_line = git(repository, "ls-remote", "origin", "refs/heads/main")
        if not remote_line:
            raise RuntimeError("REMOTE_MAIN_UNAVAILABLE: " + expected_origin)
        remote = remote_line.split()[0]
        if local == remote:
            return local
    raise RuntimeError(
        f"REMOTE_MAIN_MOVED_DURING_SYNC: {expected_origin}; refusing stale test"
    )


def launch() -> int:
    parser = argparse.ArgumentParser(
        description="Fresh remote-main only: virtual EasyChange MCP/HDMI/ESP tests"
    )
    parser.add_argument("--lab-kind", choices=["full", "optical"], default="full")
    parser.add_argument(
        "--manifest-file", default="",
        help="Optional absolute or relative JSON path to persist tested remote SHAs",
    )
    options, passthrough = parser.parse_known_args()
    reserved = ("--mcp-source", "--easychange-source")
    if any(x == key or x.startswith(key + "=")
           for x in passthrough for key in reserved):
        raise RuntimeError(
            "Pinned sources are mandatory. --mcp-source and "
            "--easychange-source cannot be overridden."
        )

    workspace = Path(__file__).resolve().parents[2]
    repos = {
        name: workspace / local
        for name, (local, _) in REPOSITORIES.items()
    }
    revisions = {}
    # Fetch and validate EVERY required remote main before materializing or running.
    for name, (local, expected_origin) in REPOSITORIES.items():
        revisions[name] = remote_main(repos[name], expected_origin)
    manifest = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "source": "verified_remote_main",
        "lab_kind": options.lab_kind,
        "commits": revisions,
    }
    print("VIRTUAL_LAB_VERIFIED_REMOTE_MAINS=" +
          json.dumps(manifest, sort_keys=True), flush=True)

    if options.manifest_file:
        destination = Path(options.manifest_file).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    scratch = workspace / "_fresh_virtual_lab_runs"
    scratch.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="ec_main_", dir=str(scratch)))
    worktrees: list[tuple[Path, Path]] = []
    try:
        snapshots: dict[str, Path] = {}
        for name, repo in repos.items():
            target = run_dir / name
            git(repo, "worktree", "add", "--detach", str(target), revisions[name])
            worktrees.append((repo, target))
            checked_out = git(target, "rev-parse", "HEAD")
            if checked_out != revisions[name]:
                raise RuntimeError(
                    f"FRESH_WORKTREE_REVISION_MISMATCH: {name}"
                )
            snapshots[name] = target

        # The MCP Python package discovers BMO tools in a sibling folder.
        # Only a FRESH BMO main checkout may satisfy that dependency, never
        # the mutable live/local BMO checkout.
        assert (snapshots["BMO_LLM_CONTROLLER"] / "mcp_server").is_dir()
        # Fail closed if a remote branch moved while the temporary snapshots
        # were being checked out. Never silently test an already-stale SHA.
        for name, repo in repos.items():
            line = git(repo, "ls-remote", "origin", "refs/heads/main")
            if not line or line.split()[0] != revisions[name]:
                raise RuntimeError(
                    f"REMOTE_MAIN_CHANGED_BEFORE_TEST: {name}; rerun to use "
                    "the new main, refusing the older snapshot"
                )

        ec = snapshots["EasyChange"]
        mcp = snapshots["MCP"]
        target_script = ec / "tests" / (
            "virtual_full_mcp_lab.py"
            if options.lab_kind == "full"
            else "virtual_optical_lab.py"
        )
        if not target_script.is_file():
            raise RuntimeError(f"LAB_SCRIPT_NOT_FOUND_IN_REMOTE_MAIN: {target_script}")
        # Current MCP main has a local 'esp32' package without protocol.py.
        # Import the verified ESP repository's package FIRST, while only its
        # pinned checkout is on PYTHONPATH. Subsequent MCP sys.path insertion
        # cannot shadow the already-loaded esp32.protocol implementation.
        bootstrap = (
            "import sys,runpy,esp32; "
            "sys.argv=sys.argv[1:]; "
            "runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        argv = [
            sys.executable, "-c", bootstrap, str(target_script),
            "--mcp-source", str(mcp),
            "--easychange-source", str(ec),
            *passthrough,
        ]
        env = os.environ.copy()
        # Current MCP imports esp32.protocol from the sibling ESP repository.
        # Expose ONLY the pinned main checkout for this module (not the local
        # mutable firmware checkout). Python's mcp package also sees fresh BMO
        # through its existing sibling-path discovery logic.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(snapshots["ESP"]), env.get("PYTHONPATH", "")]
        )
        env["VIRTUAL_LAB_VERIFIED_REVISIONS"] = json.dumps(
            revisions, sort_keys=True
        )
        print(f"VIRTUAL_LAB_EXECUTING_LATEST={target_script.name}", flush=True)
        child = subprocess.run(
            argv, cwd=str(ec), env=env, timeout=240, shell=False,
        )
        return child.returncode
    finally:
        for repo, target in reversed(worktrees):
            try:
                git(repo, "worktree", "remove", "--force", str(target), timeout=30)
            except Exception as exc:
                print(f"LAB_CLEANUP_WARNING: {target} {exc}", file=sys.stderr)
        try:
            run_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(launch())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"VIRTUAL_LAB_FRESHNESS_ABORTED: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)
