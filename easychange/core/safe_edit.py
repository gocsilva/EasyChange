from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import re
import xml.etree.ElementTree as ET


class SafeEditError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class SpanEdit:
    start: int
    end: int
    replacement: str
    operation_index: int
    operation_type: str


@dataclass
class FilePlan:
    path: str
    before: str | None
    after: str
    before_hash: str | None
    after_hash: str
    edits: list[SpanEdit]
    operations: list[dict[str, Any]]


def text_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _line_spans(text: str) -> list[tuple[int, int, int]]:
    """Return (line_start, content_end, physical_end) for each logical line."""
    raw_lines = text.splitlines(keepends=True)
    if not raw_lines and text == "":
        return []
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for raw in raw_lines:
        physical_end = offset + len(raw)
        content_end = physical_end
        if raw.endswith("\r\n"):
            content_end -= 2
        elif raw.endswith(("\n", "\r")):
            content_end -= 1
        spans.append((offset, content_end, physical_end))
        offset = physical_end
    # splitlines(keepends=True) already includes a final unterminated line.
    return spans


def _preferred_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _line_edit(original: str, line: int, content: str, index: int, kind: str) -> list[SpanEdit]:
    spans = _line_spans(original)
    if line < 1 or line > len(spans):
        raise SafeEditError("LINE_OUT_OF_RANGE", f"Line out of range: {line}", line=line)
    start, content_end, _ = spans[line - 1]
    return [SpanEdit(start, content_end, content, index, kind)]


def _range_edit(original: str, start_line: int, end_line: int, content: str, index: int, kind: str) -> list[SpanEdit]:
    spans = _line_spans(original)
    if start_line < 1 or end_line < start_line or end_line > len(spans):
        raise SafeEditError(
            "INVALID_LINE_RANGE",
            f"Invalid line range: {start_line}-{end_line}",
            start=start_line,
            end=end_line,
        )
    start = spans[start_line - 1][0]
    content_end = spans[end_line - 1][1]
    newline = _preferred_newline(original)
    replacement = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    return [SpanEdit(start, content_end, replacement, index, kind)]


def _insert_edit(original: str, line: int, content: str, index: int, kind: str) -> list[SpanEdit]:
    spans = _line_spans(original)
    if line < 1 or line > len(spans) + 1:
        raise SafeEditError("LINE_OUT_OF_RANGE", f"Line out of range: {line}", line=line)
    pos = len(original) if line == len(spans) + 1 else spans[line - 1][0]
    newline = _preferred_newline(original)
    replacement = content
    if replacement and not replacement.endswith(("\n", "\r")):
        replacement += newline
    return [SpanEdit(pos, pos, replacement, index, kind)]


def _exact_spans(
    original: str,
    old: str,
    new: str,
    index: int,
    kind: str,
    expected: int | None,
) -> list[SpanEdit]:
    if old == "":
        raise SafeEditError("EMPTY_SEARCH_TEXT", "Search text cannot be empty")
    starts = [match.start() for match in re.finditer(re.escape(old), original)]
    if expected is not None and len(starts) != expected:
        raise SafeEditError(
            "EXPECTED_OCCURRENCES_MISMATCH",
            f"Expected {expected} occurrence(s), found {len(starts)}",
            expected=expected,
            actual=len(starts),
        )
    if not starts:
        raise SafeEditError("SEARCH_TEXT_NOT_FOUND", "Search text was not found")
    return [SpanEdit(pos, pos + len(old), new, index, kind) for pos in starts]


def _anchor_region(original: str, item: dict[str, Any]) -> tuple[int, int]:
    before = str(item.get("anchor_before") or "")
    after = str(item.get("anchor_after") or "")
    lo, hi = 0, len(original)
    if before:
        matches = [m.end() for m in re.finditer(re.escape(before), original)]
        if len(matches) != 1:
            raise SafeEditError("ANCHOR_NOT_UNIQUE", "anchor_before must occur exactly once", anchor="before", occurrences=len(matches))
        lo = matches[0]
    if after:
        matches = [m.start() for m in re.finditer(re.escape(after), original)]
        matches = [pos for pos in matches if pos >= lo]
        if len(matches) != 1:
            raise SafeEditError("ANCHOR_NOT_UNIQUE", "anchor_after must occur exactly once after anchor_before", anchor="after", occurrences=len(matches))
        hi = matches[0]
    if lo > hi:
        raise SafeEditError("ANCHOR_ORDER_INVALID", "anchor_before occurs after anchor_after")
    return lo, hi


def _operation_spans(original: str, item: dict[str, Any], index: int) -> list[SpanEdit]:
    kind = str(item.get("type") or "").casefold()
    if kind == "replace_line":
        return _line_edit(original, int(item["line"]), str(item["content"]), index, kind)
    if kind == "replace_range":
        return _range_edit(original, int(item["start"]), int(item["end"]), str(item["content"]), index, kind)
    if kind == "insert":
        return _insert_edit(original, int(item["line"]), str(item["content"]), index, kind)
    if kind in {"replace_text", "replace_exact"}:
        expected = item.get("expected_occurrences")
        if expected is None and kind == "replace_exact":
            expected = 1
        return _exact_spans(
            original,
            str(item.get("old") if "old" in item else item.get("old_text") or ""),
            str(item.get("new") if "new" in item else item.get("new_text") or ""),
            index,
            kind,
            int(expected) if expected is not None else None,
        )
    if kind == "replace_anchor":
        lo, hi = _anchor_region(original, item)
        old = str(item.get("old_text") or item.get("old") or "")
        new = str(item.get("new_text") if "new_text" in item else item.get("new") or "")
        segment = original[lo:hi]
        inner = _exact_spans(segment, old, new, index, kind, int(item.get("expected_occurrences", 1)))
        return [SpanEdit(lo + span.start, lo + span.end, span.replacement, index, kind) for span in inner]
    if kind in {"insert_before", "insert_after"}:
        anchor = str(item.get("anchor") or "")
        if not anchor:
            raise SafeEditError("ANCHOR_REQUIRED", "anchor is required")
        matches = [m for m in re.finditer(re.escape(anchor), original)]
        if len(matches) != 1:
            raise SafeEditError("ANCHOR_NOT_UNIQUE", "anchor must occur exactly once", occurrences=len(matches))
        pos = matches[0].start() if kind == "insert_before" else matches[0].end()
        return [SpanEdit(pos, pos, str(item.get("content") or ""), index, kind)]
    if kind == "append":
        addition = str(item.get("content") or "")
        separator = "" if not original or original.endswith(("\n", "\r")) else _preferred_newline(original)
        return [SpanEdit(len(original), len(original), separator + addition, index, kind)]
    raise SafeEditError("UNSUPPORTED_PLANNED_EDIT", f"Unsupported planned edit: {kind}", operation=kind)


def _assert_non_overlapping(path: str, edits: list[SpanEdit]) -> None:
    ordered = sorted(edits, key=lambda edit: (edit.start, edit.end, edit.operation_index))
    for left, right in zip(ordered, ordered[1:]):
        left_zero = left.start == left.end
        right_zero = right.start == right.end
        if left_zero and right_zero and left.start == right.start:
            raise SafeEditError(
                "PATCH_OVERLAP",
                f"Multiple inserts target the same original offset in {path}",
                path=path,
                operations=[left.operation_index, right.operation_index],
            )
        if right.start < left.end or (left_zero and left.start > right.start and left.start < right.end):
            raise SafeEditError(
                "PATCH_OVERLAP",
                f"Patch operations overlap in {path}",
                path=path,
                operations=[left.operation_index, right.operation_index],
            )


def _apply_edits(original: str, edits: list[SpanEdit]) -> str:
    updated = original
    for edit in sorted(edits, key=lambda value: (value.start, value.end, value.operation_index), reverse=True):
        updated = updated[: edit.start] + edit.replacement + updated[edit.end :]
    return updated


def validate_text(path: str, text: str) -> None:
    suffix = Path(path).suffix.casefold()
    if suffix == ".py":
        compile(text, path, "exec")
    elif suffix == ".json":
        json.loads(text)
    elif suffix == ".xml":
        ET.fromstring(text)
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError:
            return
        yaml.safe_load(text)
    elif suffix == ".cs":
        _validate_csharp_structure(text)


def _validate_csharp_structure(text: str) -> None:
    """Cheap corruption guard; build/Roslyn remains authoritative when requested."""
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    opening = set(pairs.values())
    i = 0
    state = "code"
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "line_comment":
            if ch in "\r\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 1
        elif state == "string":
            if ch == "\\":
                i += 1
            elif ch == '"':
                state = "code"
        elif state == "char":
            if ch == "\\":
                i += 1
            elif ch == "'":
                state = "code"
        else:
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                i += 1
            elif ch == '"':
                state = "string"
            elif ch == "'":
                state = "char"
            elif ch in opening:
                stack.append(ch)
            elif ch in pairs:
                if not stack or stack.pop() != pairs[ch]:
                    raise SafeEditError("STRUCTURAL_VALIDATION_FAILED", f"Unbalanced delimiter {ch} in {path if False else 'C# file'}")
        i += 1
    if state in {"string", "char", "block_comment"} or stack:
        raise SafeEditError("STRUCTURAL_VALIDATION_FAILED", "Unbalanced C# structure")


def plan_file_mutations(
    originals: dict[str, str | None],
    operations: list[dict[str, Any]],
) -> dict[str, FilePlan]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(operations):
        kind = str(item.get("type") or "").casefold()
        if kind == "validate":
            continue
        path = str(item.get("path") or "")
        if not path:
            raise SafeEditError("PATH_REQUIRED", "Mutation path is required", operation_index=index)
        grouped.setdefault(path, []).append((index, item))

    plans: dict[str, FilePlan] = {}
    for path, items in grouped.items():
        original = originals.get(path)
        writes = [(index, item) for index, item in items if str(item.get("type") or "").casefold() in {"write_file", "create_file"}]
        if writes:
            if len(items) != 1:
                raise SafeEditError(
                    "PATCH_CONFLICT",
                    f"write_file/create_file cannot be mixed with other edits for {path}",
                    path=path,
                )
            index, item = writes[0]
            kind = str(item.get("type") or "").casefold()
            if kind == "create_file" and original is not None:
                raise SafeEditError("FILE_EXISTS", f"File already exists: {path}", path=path)
            after = str(item.get("content") or "")
            validate_text(path, after)
            plans[path] = FilePlan(path, original, after, text_hash(original), text_hash(after) or "", [], [item])
            continue

        if original is None:
            raise SafeEditError("FILE_NOT_FOUND", f"File not found: {path}", path=path)
        edits: list[SpanEdit] = []
        for index, item in items:
            edits.extend(_operation_spans(original, item, index))
        _assert_non_overlapping(path, edits)
        after = _apply_edits(original, edits)
        validate_text(path, after)
        plans[path] = FilePlan(
            path,
            original,
            after,
            text_hash(original),
            text_hash(after) or "",
            edits,
            [item for _, item in items],
        )
    return plans
