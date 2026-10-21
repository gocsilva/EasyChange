"""Windows handoff for updating EasyChange and reopening its current workspace."""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence


# Fresh HID/HDMI samples have exceeded 30 seconds at p95; keep the current QR
# receipt visible long enough for the MCP caller to read it before shutdown.
RESTART_GRACE_SECONDS = 45
UPDATE_TIMEOUT_SECONDS = 900


def installation_root() -> Path:
    return Path(__file__).resolve().parents[1]


def update_log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "EasyChange" / "update-restart.log"


def schedule_self_update(
    workspace: str | Path,
    *,
    repo_root: str | Path | None = None,
    executable: str | None = None,
    parent_pid: int | None = None,
    platform_name: str | None = None,
    popen_factory: Callable = subprocess.Popen,
) -> dict:
    """Start a detached updater; the worker waits for this GUI to close first."""
    if (platform_name or os.name) != "nt":
        return {"ok": False, "error": "SELF_UPDATE_REQUIRES_WINDOWS"}

    root = Path(repo_root or installation_root()).resolve()
    updater = root / "Atualizar_EasyChange.bat"
    if not updater.is_file():
        return {"ok": False, "error": "UPDATER_NOT_FOUND", "path": str(updater)}

    python = executable or sys.executable
    target_workspace = Path(workspace).resolve()
    log_path = update_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": "UPDATE_LOG_UNAVAILABLE", "detail": str(exc)}

    argv = [
        python,
        "-m",
        "easychange.self_update",
        "--worker",
        "--repo",
        str(root),
        "--workspace",
        str(target_workspace),
        "--parent-pid",
        str(parent_pid if parent_pid is not None else os.getpid()),
        "--executable",
        python,
    ]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    try:
        popen_factory(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=flags,
        )
    except (OSError, ValueError) as exc:
        log.close()
        return {"ok": False, "error": "UPDATE_WORKER_START_FAILED", "detail": str(exc)}

    log.close()
    return {
        "ok": True,
        "scheduled": True,
        "workspace": str(target_workspace),
        "restart_mode": "machine_hid",
        "restart_grace_seconds": RESTART_GRACE_SECONDS,
    }


def _wait_for_parent_exit(pid: int, timeout_seconds: int = 90) -> bool:
    """Wait through a Windows process handle so PID reuse cannot fake a handoff."""
    if os.name != "nt":
        return False
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return True
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(
            handle, int(timeout_seconds * 1000)
        )
        return result == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def run_update_worker(
    *,
    repo_root: str | Path,
    workspace: str | Path,
    parent_pid: int,
    executable: str,
    run: Callable = subprocess.run,
    wait_parent: Callable = _wait_for_parent_exit,
    sleep: Callable = time.sleep,
) -> int:
    """Update only after the GUI exits, then relaunch Machine/HID in-place."""
    root = Path(repo_root).resolve()
    target_workspace = Path(workspace).resolve()
    updater = root / "Atualizar_EasyChange.bat"
    log_path = update_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[SELF_UPDATE] repo={root} workspace={target_workspace}\n")
        if not wait_parent(parent_pid):
            log.write("[SELF_UPDATE] GUI did not exit before timeout; update cancelled.\n")
            return 6

        env = os.environ.copy()
        env["EASYCHANGE_NONINTERACTIVE"] = "1"
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(updater)]
        try:
            completed = run(
                command,
                cwd=str(root),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=UPDATE_TIMEOUT_SECONDS,
                check=False,
            )
            return_code = int(completed.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            log.write(f"[SELF_UPDATE] updater failed: {exc}\n")
            return_code = 7

        log.write(f"[SELF_UPDATE] updater_return_code={return_code}\n")
        log.write("[SELF_UPDATE] relaunching GUI in Machine/HID mode\n")

    # A short delay lets the previous console batch flush its final output.
    sleep(1)
    launch = [
        executable,
        "-m",
        "easychange.gui",
        str(target_workspace),
        "--machine",
        "--hid",
    ]
    env = os.environ.copy()
    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root) + (os.pathsep + old_pythonpath if old_pythonpath else "")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    try:
        subprocess.Popen(
            launch,
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except (OSError, ValueError) as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[SELF_UPDATE] GUI relaunch failed: {exc}\n")
        return 8
    return return_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--repo", type=Path, default=installation_root())
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--executable", default=sys.executable)
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("worker mode required")
    return run_update_worker(
        repo_root=args.repo,
        workspace=args.workspace,
        parent_pid=args.parent_pid,
        executable=args.executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
