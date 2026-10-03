from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path


_EASYCHANGE_SIDE_TEMP_RE = re.compile(
    r"^\..+\.easychange-(?:\d+|recovery)\.tmp$",
    re.IGNORECASE,
)


class MaintenanceService:
    """Safe retention/cleanup for EasyChange-owned workspace artifacts only."""

    RESULT_TTL_SECONDS = 30 * 60
    JOB_TTL_SECONDS = 24 * 60 * 60
    PROCESS_LOG_TTL_SECONDS = 6 * 60 * 60
    TEMP_TTL_SECONDS = 60 * 60
    EVIDENCE_TTL_SECONDS = 7 * 24 * 60 * 60
    MAX_RESULTS = 128
    MAX_JOBS = 128
    MAX_EVIDENCE_DIRS = 20
    DEFAULT_MAX_STATE_BYTES = 256 * 1024 * 1024
    DEFAULT_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024

    @staticmethod
    def _byte_budget(env_name: str, default: int) -> int:
        try:
            value = int(os.environ.get(env_name) or default)
        except (TypeError, ValueError):
            value = default
        return max(1024 * 1024, min(16 * 1024 * 1024 * 1024, value))

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.easy_root = self.workspace_root / ".easychange"
        self.max_state_bytes = self._byte_budget(
            "EASYCHANGE_MAX_STATE_BYTES", self.DEFAULT_MAX_STATE_BYTES
        )
        self.max_evidence_bytes = self._byte_budget(
            "EASYCHANGE_MAX_EVIDENCE_BYTES", self.DEFAULT_MAX_EVIDENCE_BYTES
        )

    def _assert_easychange_owned(self, path: Path) -> Path:
        # Use lexical absolute paths, never resolve symlinks. A .easychange
        # symlink must be unlinked, never followed into another directory.
        candidate = Path(os.path.abspath(path))
        easy = Path(os.path.abspath(self.easy_root))
        try:
            candidate.relative_to(easy)
            return candidate
        except ValueError:
            pass
        # Side-car write/recovery temp files may live beside project files.
        try:
            candidate.relative_to(self.workspace_root)
            inside_workspace = True
        except ValueError:
            inside_workspace = False
        if inside_workspace and _EASYCHANGE_SIDE_TEMP_RE.fullmatch(candidate.name):
            return candidate
        raise ValueError(f"REFUSING_NON_EASYCHANGE_PATH: {candidate}")

    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            if path.is_file() or path.is_symlink():
                return path.stat().st_size
        except OSError:
            return 0
        total = 0
        try:
            for item in path.rglob("*"):
                if not item.is_file() or item.is_symlink():
                    continue
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        return total

    def _workspace_side_temps(self) -> list[Path]:
        output: list[Path] = []
        skip_dirs = {
            ".git", ".easychange", ".venv", "venv", "node_modules",
            "bin", "obj", "dist", "build", "__pycache__",
        }
        try:
            for root, dirs, files in os.walk(self.workspace_root):
                dirs[:] = [name for name in dirs if name not in skip_dirs]
                base = Path(root)
                for name in files:
                    if name.endswith(".tmp") and _EASYCHANGE_SIDE_TEMP_RE.fullmatch(name):
                        output.append(base / name)
        except OSError:
            pass
        return output

    def stats(self, *, include_side_temps: bool = False) -> dict:
        categories: dict[str, dict] = {}
        total_bytes = 0
        total_files = 0
        if self.easy_root.exists() or self.easy_root.is_symlink():
            if self.easy_root.is_symlink():
                categories["easychange_symlink"] = {
                    "bytes": self._path_size(self.easy_root),
                    "files": 1,
                }
                total_bytes += categories["easychange_symlink"]["bytes"]
                total_files += 1
                children = []
            else:
                try:
                    children = list(self.easy_root.iterdir())
                except OSError:
                    children = []
            for child in children:
                size = self._path_size(child)
                count = 0
                try:
                    count = 1 if child.is_file() else sum(
                        1 for item in child.rglob("*") if item.is_file()
                    )
                except OSError:
                    pass
                categories[child.name] = {"bytes": size, "files": count}
                total_bytes += size
                total_files += count
        if include_side_temps:
            side_temps = self._workspace_side_temps()
            side_bytes = sum(self._path_size(path) for path in side_temps)
            if side_temps:
                categories["sidecar_temps"] = {"bytes": side_bytes, "files": len(side_temps)}
                total_bytes += side_bytes
                total_files += len(side_temps)
        return {
            "root": str(self.easy_root),
            "exists": self.easy_root.exists(),
            "total_bytes": total_bytes,
            "total_files": total_files,
            "categories": categories,
        }

    def _remove(self, path: Path, report: dict, category: str, *, dry_run: bool) -> None:
        target = self._assert_easychange_owned(path)
        size = self._path_size(target)
        if not target.exists() and not target.is_symlink():
            return
        if not dry_run:
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as exc:
                report["errors"].append({
                    "path": str(target), "category": category, "error": str(exc)[:300],
                })
                return
        report["removed_files"] += 1
        report["reclaimed_bytes"] += size
        report["categories"][category] = report["categories"].get(category, 0) + size
        if len(report["removed"]) < 100:
            report["removed"].append(str(target))

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    def _evidence_dirs(self) -> list[Path]:
        root = self.easy_root / "evidence"
        try:
            return [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]
        except OSError:
            return []

    def _pressure_candidates(
        self,
        *,
        active_process_ids: set[str],
        referenced_blobs: set[str],
    ) -> list[tuple[float, Path, str]]:
        """Return disposable EasyChange-owned entries, oldest first.

        Journal/session/config/locks and referenced blobs are intentionally absent.
        """
        candidates: list[tuple[float, Path, str]] = []

        results_root = self.easy_root / "results"
        try:
            for path in results_root.glob("Q*.json.zlib"):
                candidates.append((self._mtime(path), path, "results_pressure"))
        except OSError:
            pass

        for path in self._evidence_dirs():
            candidates.append((self._mtime(path), path, "evidence_pressure"))

        try:
            for path in self.easy_root.rglob("*.tmp"):
                candidates.append((self._mtime(path), path, "temp_pressure"))
        except OSError:
            pass

        jobs_root = self.easy_root / "jobs"
        try:
            for path in jobs_root.glob("*.json"):
                state = ""
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        state = str(value.get("state") or "").upper()
                except (OSError, json.JSONDecodeError):
                    state = "CORRUPT"
                if state == "RUNNING" or path.stem in active_process_ids:
                    continue
                candidates.append((self._mtime(path), path, "jobs_pressure"))
        except OSError:
            pass

        process_root = self.easy_root / "processes"
        try:
            for path in process_root.glob("*.*"):
                if path.suffix.lower() not in {".out", ".err"}:
                    continue
                if path.stem in active_process_ids:
                    continue
                candidates.append((self._mtime(path), path, "process_logs_pressure"))
        except OSError:
            pass

        blob_root = self.easy_root / "blobs"
        try:
            for path in blob_root.glob("*.zlib"):
                if path.stem not in referenced_blobs:
                    candidates.append((self._mtime(path), path, "orphan_blobs_pressure"))
        except OSError:
            pass

        # A path can appear in a TTL category and a pressure category only
        # before deletion; de-duplicate lexically here.
        unique: dict[str, tuple[float, Path, str]] = {}
        for item in candidates:
            key = os.path.normcase(os.path.abspath(item[1]))
            unique.setdefault(key, item)
        return sorted(unique.values(), key=lambda item: (item[0], str(item[1]).casefold()))

    def _enforce_disk_budgets(
        self,
        report: dict,
        *,
        active_process_ids: set[str],
        referenced_blobs: set[str],
        dry_run: bool,
    ) -> None:
        evidence_before = sum(self._path_size(path) for path in self._evidence_dirs())
        projected_evidence = evidence_before
        evidence_removed: set[str] = set()

        for path in sorted(self._evidence_dirs(), key=lambda item: (self._mtime(item), str(item))):
            if projected_evidence <= self.max_evidence_bytes:
                break
            size = self._path_size(path)
            self._remove(path, report, "evidence_budget", dry_run=dry_run)
            projected_evidence = max(0, projected_evidence - size)
            evidence_removed.add(os.path.normcase(os.path.abspath(path)))

        state_before = int(self.stats().get("total_bytes") or 0)
        # In dry-run, prior TTL/LRU removals are still present physically. Use
        # reclaimed_bytes to model the projected size instead of deleting.
        projected_state = (
            max(0, state_before - int(report.get("reclaimed_bytes") or 0))
            if dry_run
            else state_before
        )

        for _mtime, path, category in self._pressure_candidates(
            active_process_ids=active_process_ids,
            referenced_blobs=referenced_blobs,
        ):
            if projected_state <= self.max_state_bytes:
                break
            key = os.path.normcase(os.path.abspath(path))
            if key in evidence_removed:
                continue
            if not path.exists() and not path.is_symlink():
                continue
            size = self._path_size(path)
            self._remove(path, report, category, dry_run=dry_run)
            projected_state = max(0, projected_state - size)

        actual_after = int(self.stats().get("total_bytes") or 0)
        if dry_run:
            projected_after = projected_state
        else:
            projected_after = actual_after
        report["disk_budget"] = {
            "state_max_bytes": self.max_state_bytes,
            "evidence_max_bytes": self.max_evidence_bytes,
            "state_before_bytes": state_before,
            "state_after_bytes": actual_after,
            "state_projected_after_bytes": projected_after,
            "evidence_before_bytes": evidence_before,
            "evidence_projected_after_bytes": projected_evidence,
            "over_budget": projected_after > self.max_state_bytes,
            "essential_state_pressure_bytes": max(
                0, projected_after - self.max_state_bytes
            ),
        }

    def automatic_cleanup(
        self,
        *,
        referenced_blobs: set[str] | None = None,
        active_process_ids: set[str] | None = None,
        now: float | None = None,
        dry_run: bool = False,
        scan_workspace_temps: bool = False,
    ) -> dict:
        now = time.time() if now is None else float(now)
        cleanup_orphan_blobs = referenced_blobs is not None
        referenced_blobs = set(referenced_blobs or ())
        active_process_ids = set(active_process_ids or ())
        report = {
            "mode": "automatic",
            "dry_run": bool(dry_run),
            "removed_files": 0,
            "reclaimed_bytes": 0,
            "categories": {},
            "removed": [],
            "errors": [],
        }
        if self.easy_root.is_symlink():
            report["errors"].append({
                "path": str(self.easy_root),
                "category": "safety",
                "error": "EASYCHANGE_ROOT_SYMLINK_AUTO_CLEANUP_SKIPPED",
            })
            return {**report, "before": self.stats(), "after": self.stats()}
        if not self.easy_root.exists():
            return {**report, "before": self.stats(), "after": self.stats()}

        before = self.stats()

        # Stale atomic-write leftovers under .easychange.
        try:
            for path in self.easy_root.rglob("*.tmp"):
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if age > self.TEMP_TTL_SECONDS:
                    self._remove(path, report, "temp", dry_run=dry_run)
        except OSError:
            pass

        # Full workspace scans are opt-in; periodic maintenance stays O(.easychange).
        if scan_workspace_temps:
            for path in self._workspace_side_temps():
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if age > self.TEMP_TTL_SECONDS:
                    self._remove(path, report, "sidecar_temp", dry_run=dry_run)

        # Durable results: same retention semantics as DurableResultStore.
        results_root = self.easy_root / "results"
        try:
            results = sorted(
                results_root.glob("Q*.json.zlib"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            results = []
        for position, path in enumerate(results):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if position >= self.MAX_RESULTS or age > self.RESULT_TTL_SECONDS:
                self._remove(path, report, "results", dry_run=dry_run)

        # Jobs: preserve RUNNING jobs; terminal/stale jobs are bounded.
        jobs_root = self.easy_root / "jobs"
        try:
            jobs = sorted(
                jobs_root.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            jobs = []
        terminal_position = 0
        for path in jobs:
            state = ""
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                state = str(payload.get("state") or "").upper() if isinstance(payload, dict) else ""
            except (OSError, json.JSONDecodeError):
                state = "CORRUPT"
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = self.JOB_TTL_SECONDS + 1
            if state == "RUNNING" and path.stem in active_process_ids:
                continue
            if state == "RUNNING" and age <= self.JOB_TTL_SECONDS:
                continue
            if terminal_position >= self.MAX_JOBS or age > self.JOB_TTL_SECONDS:
                self._remove(path, report, "jobs", dry_run=dry_run)
            terminal_position += 1

        # Process log files are disposable once their process is no longer active.
        process_root = self.easy_root / "processes"
        try:
            process_files = list(process_root.glob("*.*"))
        except OSError:
            process_files = []
        for path in process_files:
            if path.suffix.lower() not in {".out", ".err"}:
                continue
            process_id = path.stem
            if process_id in active_process_ids:
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > self.PROCESS_LOG_TTL_SECONDS:
                self._remove(path, report, "process_logs", dry_run=dry_run)

        # Evidence: bounded by both age and count.
        evidence_root = self.easy_root / "evidence"
        try:
            evidence_dirs = sorted(
                [path for path in evidence_root.iterdir() if path.is_dir()],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            evidence_dirs = []
        for position, path in enumerate(evidence_dirs):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if position >= self.MAX_EVIDENCE_DIRS or age > self.EVIDENCE_TTL_SECONDS:
                self._remove(path, report, "evidence", dry_run=dry_run)

        # Content-addressed journal blobs not referenced by journal/redo are safe to drop.
        blob_root = self.easy_root / "blobs"
        try:
            blobs = list(blob_root.glob("*.zlib"))
        except OSError:
            blobs = []
        if cleanup_orphan_blobs:
            for path in blobs:
                if path.stem not in referenced_blobs:
                    self._remove(path, report, "orphan_blobs", dry_run=dry_run)

        self._enforce_disk_budgets(
            report,
            active_process_ids=active_process_ids,
            referenced_blobs=referenced_blobs,
            dry_run=dry_run,
        )

        report["before"] = before
        report["after"] = before if dry_run else self.stats(include_side_temps=scan_workspace_temps)
        return report

    def purge_all(self, *, dry_run: bool = False) -> dict:
        """Remove every EasyChange-owned artifact from this workspace.

        This must be called after the active CommandService is closed.
        """
        before = self.stats(include_side_temps=True)
        report = {
            "mode": "full",
            "dry_run": bool(dry_run),
            "removed_files": 0,
            "reclaimed_bytes": 0,
            "categories": {},
            "removed": [],
            "errors": [],
            "before": before,
        }
        if self.easy_root.exists() or self.easy_root.is_symlink():
            self._remove(self.easy_root, report, "easychange_root", dry_run=dry_run)
        for path in self._workspace_side_temps():
            self._remove(path, report, "sidecar_temp", dry_run=dry_run)
        report["after"] = before if dry_run else self.stats(include_side_temps=True)
        return report
