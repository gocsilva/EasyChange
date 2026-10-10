from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


STAMP_NAME = ".easychange-venv-state.json"
DEFAULT_INSTALL_SPEC = ".[gui]"
_MANIFEST_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "poetry.lock",
    "uv.lock",
    "pdm.lock",
    "Pipfile",
    "Pipfile.lock",
}
_MANIFEST_GLOBS = ("requirements*.txt", "constraints*.txt")


def dependency_manifests(root: Path) -> list[Path]:
    root = Path(root).resolve()
    found: set[Path] = set()
    for name in _MANIFEST_NAMES:
        path = root / name
        if path.is_file():
            found.add(path)
    for pattern in _MANIFEST_GLOBS:
        found.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def required_python(root: Path) -> tuple[int, int]:
    pyproject = Path(root) / "pyproject.toml"
    if not pyproject.is_file():
        return (3, 10)
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (3, 10)
    match = re.search(
        r"(?mi)^\s*requires-python\s*=\s*[\"']>=\s*(\d+)\.(\d+)",
        text,
    )
    if not match:
        return (3, 10)
    return int(match.group(1)), int(match.group(2))


def dependency_fingerprint(root: Path, install_spec: str = DEFAULT_INSTALL_SPEC) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    digest.update(b"easychange-venv-state-v1\0")
    digest.update(str(install_spec).encode("utf-8"))
    digest.update(b"\0")
    for path in dependency_manifests(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        # Dependency lockfiles may be megabytes long. Their fingerprint is
        # streamed for a stable digest without materializing each file.
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def venv_python(venv: Path) -> Path:
    return Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def stamp_path(venv: Path) -> Path:
    return Path(venv) / STAMP_NAME


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    return runner(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def interpreter_info(
    python: Path | str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any] | None:
    result = _run(
        [
            str(python),
            "-c",
            (
                "import json,sys;"
                "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor,sys.version_info.micro],"
                "'executable':sys.executable}))"
            ),
        ],
        timeout=20,
        runner=runner,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    version = value.get("version")
    if not isinstance(version, list) or len(version) < 2:
        return None
    return value


def dependencies_healthy(
    python: Path | str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    result = _run(
        [
            str(python),
            "-c",
            (
                "import pip,easychange,PySide6,qrcode;"
                "print('EASYCHANGE_VENV_OK')"
            ),
        ],
        timeout=30,
        runner=runner,
    )
    return result.returncode == 0 and "EASYCHANGE_VENV_OK" in (result.stdout or "")


def read_stamp(venv: Path) -> dict[str, Any] | None:
    path = stamp_path(venv)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def evaluate(
    root: Path,
    *,
    install_spec: str = DEFAULT_INSTALL_SPEC,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    root = Path(root).resolve()
    venv = root / ".venv"
    python = venv_python(venv)
    fingerprint = dependency_fingerprint(root, install_spec)
    required = required_python(root)
    info = interpreter_info(python, runner=runner) if python.exists() else None
    stamp = read_stamp(venv)
    reasons: list[str] = []

    if info is None:
        reasons.append("venv_missing_or_python_broken")
    else:
        version = tuple(int(item) for item in info.get("version", [])[:2])
        if version < required:
            reasons.append(
                f"python_too_old:{version[0]}.{version[1]}<"
                f"{required[0]}.{required[1]}"
            )
        if not dependencies_healthy(python, runner=runner):
            reasons.append("dependencies_unhealthy")

    if stamp is None:
        reasons.append("state_stamp_missing")
    else:
        if stamp.get("dependency_fingerprint") != fingerprint:
            reasons.append("dependency_manifest_changed")
        if str(stamp.get("install_spec") or "") != install_spec:
            reasons.append("install_spec_changed")
        if info is not None:
            current_version = ".".join(str(item) for item in info.get("version", [])[:2])
            if str(stamp.get("python_major_minor") or "") != current_version:
                reasons.append("venv_python_changed")

    return {
        "root": str(root),
        "venv": str(venv),
        "python": str(python),
        "required_python": f"{required[0]}.{required[1]}",
        "dependency_fingerprint": fingerprint,
        "manifest_files": [
            path.relative_to(root).as_posix()
            for path in dependency_manifests(root)
        ],
        "install_spec": install_spec,
        "python_info": info,
        "stamp": stamp,
        "needs_sync": bool(reasons),
        "reasons": reasons,
    }


def _bootstrap_python_ok(
    python: Path | str,
    minimum: tuple[int, int],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    info = interpreter_info(python, runner=runner)
    if info is None:
        return False
    version = tuple(int(item) for item in info.get("version", [])[:2])
    return version >= minimum


def ensure(
    root: Path,
    *,
    install_spec: str = DEFAULT_INSTALL_SPEC,
    force: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    root = Path(root).resolve()
    venv = root / ".venv"
    target_python = venv_python(venv)
    required = required_python(root)
    state = evaluate(root, install_spec=install_spec, runner=runner)
    if force and "forced" not in state["reasons"]:
        state["reasons"].append("forced")
        state["needs_sync"] = True

    if not state["needs_sync"]:
        return {**state, "action": "unchanged", "changed": False}

    if dry_run:
        return {**state, "action": "would_sync", "changed": False}

    bootstrap = Path(sys.executable)
    if not _bootstrap_python_ok(bootstrap, required, runner=runner):
        raise RuntimeError(
            "BOOTSTRAP_PYTHON_TOO_OLD:"
            f"need>={required[0]}.{required[1]} executable={bootstrap}"
        )

    target_info = interpreter_info(target_python, runner=runner) if target_python.exists() else None
    recreate = target_info is None
    if target_info is not None:
        target_version = tuple(int(item) for item in target_info.get("version", [])[:2])
        recreate = target_version < required

    if recreate:
        if venv.exists():
            shutil.rmtree(venv)
        created = _run(
            [str(bootstrap), "-m", "venv", str(venv)],
            cwd=root,
            timeout=180,
            runner=runner,
        )
        if created.returncode != 0:
            raise RuntimeError(
                "VENV_CREATE_FAILED:"
                + ((created.stderr or created.stdout or "").strip()[-1200:])
            )

    target_python = venv_python(venv)
    if interpreter_info(target_python, runner=runner) is None:
        raise RuntimeError("VENV_PYTHON_UNHEALTHY_AFTER_CREATE")

    pip_check = _run(
        [str(target_python), "-m", "pip", "--version"],
        cwd=root,
        timeout=30,
        runner=runner,
    )
    if pip_check.returncode != 0:
        ensurepip = _run(
            [str(target_python), "-m", "ensurepip", "--upgrade"],
            cwd=root,
            timeout=120,
            runner=runner,
        )
        if ensurepip.returncode != 0:
            raise RuntimeError(
                "VENV_PIP_REPAIR_FAILED:"
                + ((ensurepip.stderr or ensurepip.stdout or "").strip()[-1200:])
            )

    upgrade = _run(
        [str(target_python), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=root,
        timeout=300,
        runner=runner,
    )
    if upgrade.returncode != 0:
        raise RuntimeError(
            "PIP_UPGRADE_FAILED:"
            + ((upgrade.stderr or upgrade.stdout or "").strip()[-1200:])
        )

    install = _run(
        [str(target_python), "-m", "pip", "install", "-e", install_spec],
        cwd=root,
        timeout=900,
        runner=runner,
    )
    if install.returncode != 0:
        raise RuntimeError(
            "DEPENDENCY_SYNC_FAILED:"
            + ((install.stderr or install.stdout or "").strip()[-2000:])
        )

    if not dependencies_healthy(target_python, runner=runner):
        raise RuntimeError("DEPENDENCY_HEALTHCHECK_FAILED")

    final_info = interpreter_info(target_python, runner=runner)
    assert final_info is not None
    fingerprint = dependency_fingerprint(root, install_spec)
    stamp = {
        "schema": 1,
        "dependency_fingerprint": fingerprint,
        "install_spec": install_spec,
        "python_major_minor": ".".join(
            str(item) for item in final_info.get("version", [])[:2]
        ),
        "python_executable": str(final_info.get("executable") or target_python),
        "updated_at": time.time(),
    }
    _atomic_write_json(stamp_path(venv), stamp)
    final = evaluate(root, install_spec=install_spec, runner=runner)
    if final["needs_sync"]:
        raise RuntimeError(
            "VENV_STILL_REQUIRES_SYNC_AFTER_UPDATE:"
            + ",".join(final["reasons"])
        )
    return {
        **final,
        "action": "recreated_and_synced" if recreate else "synced",
        "changed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create/repair/sync EasyChange .venv only when required."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="EasyChange repository root",
    )
    parser.add_argument("--install-spec", default=DEFAULT_INSTALL_SPEC)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = ensure(
            Path(args.root),
            install_spec=args.install_spec,
            force=args.force,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"[ERRO] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        action = result["action"]
        reasons = ", ".join(result.get("reasons") or []) or "none"
        print(f"[VENV] action={action} reasons={reasons}")
        print(f"[VENV] python={result['python']} required>={result['required_python']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
