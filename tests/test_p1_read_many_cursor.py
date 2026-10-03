from __future__ import annotations

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _line_numbers(file_payload):
    numbers = []
    for line in file_payload.get("lines") or []:
        prefix = str(line).split("|", 1)[0]
        numbers.append(int(prefix))
    return numbers


def test_read_many_cursor_recovers_budget_truncated_pages_without_duplicates(tmp_path):
    for file_index in range(3):
        content = "\n".join(
            f"{file_index}-{line:04d}-" + ("x" * 240)
            for line in range(1, 301)
        ) + "\n"
        (tmp_path / f"big-{file_index}.txt").write_text(content, encoding="utf-8")

    service = CommandService(Workspace.open(tmp_path))
    try:
        paths = [f"big-{index}.txt" for index in range(3)]
        cursor = None
        seen = {path: [] for path in paths}
        pages = 0

        while True:
            args = [*paths, "--count", "300", "--max-bytes", "32768"]
            if cursor:
                args += ["--cursor", cursor]
            result = service.execute_tokens("read-many", args, raw="test read-many", persist=False)
            assert result.ok is True
            data = result.data
            pages += 1
            for payload in data["files"]:
                seen[payload["path"]].extend(_line_numbers(payload))
            if data["complete"]:
                assert data["next_cursor"] is None
                break
            cursor = data["next_cursor"]
            assert cursor.startswith("v1:")
            assert pages < 30

        assert pages > 3
        for path in paths:
            assert seen[path] == list(range(1, 301))
    finally:
        service.close()


def test_structured_read_many_accepts_returned_cursor(tmp_path):
    content = "\n".join(f"{line:04d}-" + ("z" * 260) for line in range(1, 241)) + "\n"
    (tmp_path / "large.txt").write_text(content, encoding="utf-8")

    service = CommandService(Workspace.open(tmp_path))
    try:
        first = service._execute_structured({
            "op": "operation",
            "type": "read_many",
            "paths": ["large.txt"],
            "count": 240,
            "max_bytes": 32768,
        })
        assert first.ok is True
        first_data = first.data["results"][0]["data"]
        assert first_data["complete"] is False
        cursor = first_data["next_cursor"]
        assert cursor

        second = service._execute_structured({
            "op": "operation",
            "type": "read_many",
            "paths": ["large.txt"],
            "count": 240,
            "max_bytes": 32768,
            "cursor": cursor,
        })
        assert second.ok is True
        second_data = second.data["results"][0]["data"]
        first_lines = _line_numbers(first_data["files"][0])
        second_lines = _line_numbers(second_data["files"][0])
        assert first_lines[-1] + 1 == second_lines[0]
    finally:
        service.close()
