"""Windows handoff for updating EasyChange and reopening its current workspace."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence


# Fresh HID/HDMI samples have exceeded 30 seconds at p95; keep the current QR
# receipt visible long enough for the MCP caller to read it before shutdown.
RESTART_GRACE_SECONDS = 45
UPDATE_TIMEOUT_SECONDS = 900
UPDATE_RECEIPT_SCHEMA = "easychange.update_receipt.v1"
MAX_UPDATE_RECEIPT_BYTES = 64 * 1024
_SCHEDULE_LOCK = threading.RLock()


def installation_root() -> Path:
    return Path(__file__).resolve().parents[1]


def update_log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "EasyChange" / "update-restart.log"


def update_receipt_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "EasyChange" / "update-receipt.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_update_receipt(receipt: dict) -> None:
    """Atomically persist a small receipt outside the disposable Git checkout."""
    path = update_receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".update-receipt-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_update_receipt_bounded(path: Path) -> dict:
    """Read a local update receipt without trusting its on-disk byte size."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_UPDATE_RECEIPT_BYTES + 1)
    if len(raw) > MAX_UPDATE_RECEIPT_BYTES:
        raise ValueError("UPDATE_RECEIPT_SIZE_LIMIT")
    return json.loads(raw.decode("utf-8"))


def _read_git_revisions(repo_root: str | Path) -> dict:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(repo_root).resolve()), "rev-parse", "HEAD", "origin/main"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        revisions = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(revisions) != 2:
            raise ValueError("EXPECTED_HEAD_AND_ORIGIN_MAIN")
        head, origin_main = revisions
        return {
            "head_sha": head,
            "origin_main_sha": origin_main,
            "matches_origin_main": head == origin_main,
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"revision_error": f"{type(exc).__name__}:{str(exc)[:120]}"}


def _update_receipt_activity(receipt: dict, *, now: datetime | None = None) -> tuple[bool, bool]:
    """Bound stale SCHEDULED/RUNNING receipts using the handoff timeout."""
    state = receipt.get("state")
    if state not in {"SCHEDULED", "RUNNING"}:
        return False, False
    try:
        started = datetime.fromisoformat(str(receipt.get("started_at_utc") or ""))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = (current - started).total_seconds()
        maximum = (
            RESTART_GRACE_SECONDS + 90 + 30
            if state == "SCHEDULED"
            else UPDATE_TIMEOUT_SECONDS + 120
        )
        fresh = -5 <= age <= maximum
        return fresh, not fresh
    except (ValueError, TypeError, OverflowError):
        return False, True


def read_update_status(*, repo_root: str | Path | None = None) -> dict:
    """Return the latest self-update receipt and compare it with the checkout."""
    path = update_receipt_path()
    try:
        receipt = _read_update_receipt_bounded(path)
    except FileNotFoundError:
        return {"receipt_available": False, "state": "NO_UPDATE_RECEIPT"}
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "receipt_available": False,
            "state": "UPDATE_RECEIPT_UNREADABLE",
            "error": f"{type(exc).__name__}:{str(exc)[:120]}",
        }
    if not isinstance(receipt, dict) or receipt.get("schema") != UPDATE_RECEIPT_SCHEMA:
        return {"receipt_available": False, "state": "UPDATE_RECEIPT_INVALID"}

    revisions = _read_git_revisions(repo_root or installation_root())
    current_sha = revisions.get("head_sha")
    origin_main_sha = revisions.get("origin_main_sha")
    installed_sha = receipt.get("installed_git_sha")
    update_in_progress, update_stale = _update_receipt_activity(receipt)
    return {
        "receipt_available": True,
        "state": receipt.get("state"),
        "updater_return_code": receipt.get("updater_return_code"),
        "installed_git_sha": installed_sha,
        "origin_main_sha": origin_main_sha or receipt.get("origin_main_sha"),
        "current_git_sha": current_sha,
        "current_matches_receipt": bool(installed_sha and current_sha == installed_sha),
        "current_matches_origin_main": revisions.get("matches_origin_main"),
        "revision_error": revisions.get("revision_error") or receipt.get("revision_error"),
        "relaunch_state": receipt.get("relaunch_state"),
        "workspace": receipt.get("workspace"),
        "worker_pid": receipt.get("worker_pid"),
        "schedule_error": receipt.get("schedule_error"),
        "finished_at_utc": receipt.get("finished_at_utc"),
        "started_at_utc": receipt.get("started_at_utc"),
        "update_in_progress": update_in_progress,
        "update_stale": update_stale,
        "relaunch_verified_by_mcp": False,
    }


def schedule_self_update(
    workspace: str | Path,
    *,
    repo_root: str | Path | None = None,
    executable: str | None = None,
    parent_pid: int | None = None,
    platform_name: str | None = None,
    popen_factory: Callable = subprocess.Popen,
) -> dict:
    """Schedule at most one updater per GUI process and active receipt."""
    if (platform_name or os.name) != "nt":
        return {"ok": False, "error": "SELF_UPDATE_REQUIRES_WINDOWS"}
    try:
        with _SCHEDULE_LOCK, _schedule_process_lock():
            return _schedule_self_update_once(
                workspace, repo_root=repo_root, executable=executable,
                parent_pid=parent_pid, platform_name=platform_name,
                popen_factory=popen_factory,
            )
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}


@contextmanager
def _schedule_process_lock(path: str | Path | None = None, *, timeout: float = 2.0):
    """Serialize update receipt check-and-spawn across GUI processes."""
    lock_path = Path(path) if path is not None else update_receipt_path().with_name(
        ".update-schedule.lock"
    )
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError("UPDATE_SCHEDULER_LOCK_UNAVAILABLE") from exc
    if lock_path.is_symlink():
        raise RuntimeError("UPDATE_SCHEDULER_UNSAFE_LOCK_PATH")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("UPDATE_SCHEDULER_LOCK_UNAVAILABLE") from exc
    try:
        handle = os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        identity = os.fstat(handle.fileno())
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise RuntimeError("UPDATE_SCHEDULER_UNSAFE_LOCK_TYPE")
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            handle.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if (exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
                        and getattr(exc, "winerror", None) not in (33, 36)):
                    raise RuntimeError("UPDATE_SCHEDULER_LOCK_UNAVAILABLE") from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("UPDATE_SCHEDULER_LOCK_BUSY") from exc
                time.sleep(min(0.05, remaining))
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(bytes([0]))
                handle.flush()
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _active_update_handoff(root: Path, workspace: Path) -> dict | None:
    try:
        existing = _read_update_receipt_bounded(update_receipt_path())
        if not isinstance(existing, dict):
            return None
        if (existing.get("schema") != UPDATE_RECEIPT_SCHEMA
                or existing.get("repo_root") != str(root)
                or existing.get("state") not in {"SCHEDULED", "RUNNING"}):
            return None
        active, _stale = _update_receipt_activity(existing)
        if not active:
            return None
        return existing
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _schedule_self_update_once(
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
    existing = _active_update_handoff(root, target_workspace)
    if existing is not None:
        return {
            "ok": True, "scheduled": True, "already_scheduled": True,
            "worker_pid": existing.get("worker_pid"),
            "workspace": existing.get("workspace") or str(target_workspace),
            "restart_mode": "machine_hid",
            "restart_grace_seconds": RESTART_GRACE_SECONDS,
        }
    log_path = update_log_path()
    scheduled_at = _utc_now()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": "UPDATE_LOG_UNAVAILABLE", "detail": str(exc)}

    # Persist handoff intent before spawning the detached worker. If Windows
    # accepts Popen but the child cannot import/start (or the GUI restarts
    # before it writes its RUNNING receipt), status must still distinguish an
    # accepted handoff from a command that never scheduled an update.
    revisions = _read_git_revisions(root)
    scheduled_receipt = {
        "schema": UPDATE_RECEIPT_SCHEMA,
        "state": "SCHEDULED",
        "updater_return_code": None,
        "installed_git_sha": revisions.get("head_sha"),
        "origin_main_sha": revisions.get("origin_main_sha"),
        "relaunch_state": "PENDING",
        "workspace": str(target_workspace),
        "repo_root": str(root),
        "worker_pid": None,
        "started_at_utc": scheduled_at,
        "finished_at_utc": None,
    }
    try:
        _write_update_receipt(scheduled_receipt)
    except OSError as exc:
        log.close()
        return {"ok": False, "error": "UPDATE_RECEIPT_UNAVAILABLE", "detail": str(exc)}

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
        worker = popen_factory(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=flags,
        )
    except (OSError, ValueError) as exc:
        scheduled_receipt.update(
            state="SCHEDULE_FAILED",
            relaunch_state="NOT_STARTED",
            schedule_error=f"{type(exc).__name__}:{str(exc)[:120]}",
            finished_at_utc=_utc_now(),
        )
        try:
            _write_update_receipt(scheduled_receipt)
        except OSError:
            pass
        log.close()
        return {"ok": False, "error": "UPDATE_WORKER_START_FAILED", "detail": str(exc)}

    scheduled_receipt["worker_pid"] = int(getattr(worker, "pid", 0) or 0) or None
    try:
        receipt_path = update_receipt_path()
        latest = _read_update_receipt_bounded(receipt_path)
        if (
            isinstance(latest, dict)
            and latest.get("schema") == UPDATE_RECEIPT_SCHEMA
            and latest.get("state") == "SCHEDULED"
            and latest.get("started_at_utc") == scheduled_at
        ):
            latest["worker_pid"] = scheduled_receipt["worker_pid"]
            _write_update_receipt(latest)
    except OSError:
        # The pre-spawn receipt is already durable. The worker will replace it
        # with RUNNING and its final update outcome when it begins. Do not
        # overwrite a newer worker receipt if the detached child started fast.
        pass
    except (UnicodeError, ValueError, json.JSONDecodeError):
        pass
    log.close()
    return {
        "ok": True,
        "scheduled": True,
        "worker_pid": scheduled_receipt["worker_pid"],
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
        # ERROR_INVALID_PARAMETER (87): the PID no longer exists.
        # ERROR_ACCESS_DENIED (5), or any other failure, does NOT
        # prove the GUI exited. Updating files underneath a running
        # EasyChange instance can corrupt the handoff or freeze its UI.
        return ctypes.windll.kernel32.GetLastError() == 87
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
    receipt = {
        "schema": UPDATE_RECEIPT_SCHEMA,
        "state": "RUNNING",
        "updater_return_code": None,
        "installed_git_sha": None,
        "origin_main_sha": None,
        "relaunch_state": "PENDING",
        "workspace": str(target_workspace),
        "repo_root": str(root),
        "worker_pid": os.getpid(),
        "started_at_utc": _utc_now(),
        "finished_at_utc": None,
    }

    def persist_receipt(log=None):
        try:
            _write_update_receipt(receipt)
        except OSError as exc:
            if log is not None:
                log.write(f"[SELF_UPDATE] receipt_write_failed={type(exc).__name__}\n")

    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[SELF_UPDATE] repo={root} workspace={target_workspace}\n")
        persist_receipt(log)
        if not wait_parent(parent_pid):
            log.write("[SELF_UPDATE] GUI did not exit before timeout; update cancelled.\n")
            receipt.update(
                state="CANCELLED_PARENT_TIMEOUT",
                updater_return_code=6,
                finished_at_utc=_utc_now(),
            )
            persist_receipt(log)
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
                # The updater runs from a detached background Python worker.
                # Without this flag Windows creates a *visible* cmd.exe window
                # over EasyChange, steals HID keyboard focus and makes MCP
                # capture only the previous EC2 QR for tens of seconds.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            return_code = int(completed.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            log.write(f"[SELF_UPDATE] updater failed: {exc}\n")
            return_code = 7

        log.write(f"[SELF_UPDATE] updater_return_code={return_code}\n")
        revisions = _read_git_revisions(root)
        receipt.update(
            state=(
                "UPDATED"
                if return_code == 0 and revisions.get("matches_origin_main") is True
                else "UPDATED_UNVERIFIED"
                if return_code == 0
                else "UPDATE_FAILED"
            ),
            updater_return_code=return_code,
            installed_git_sha=revisions.get("head_sha"),
            origin_main_sha=revisions.get("origin_main_sha"),
            revision_error=revisions.get("revision_error"),
            finished_at_utc=_utc_now(),
        )
        persist_receipt(log)
        if revisions.get("head_sha"):
            log.write(f"[SELF_UPDATE] installed_git_sha={revisions['head_sha']}\n")
        if revisions.get("origin_main_sha"):
            log.write(f"[SELF_UPDATE] origin_main_sha={revisions['origin_main_sha']}\n")
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
        receipt["relaunch_state"] = "FAILED"
        receipt["relaunch_error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
        try:
            _write_update_receipt(receipt)
        except OSError:
            pass
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[SELF_UPDATE] GUI relaunch failed: {exc}\n")
        return 8
    receipt["relaunch_state"] = "PROCESS_STARTED"
    persist_receipt()
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
