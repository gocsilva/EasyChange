from __future__ import annotations

import pytest

from easychange.core.safe_edit import SafeEditError, plan_file_mutations, validate_text


def test_same_file_line_operations_use_original_snapshot_coordinates():
    original = "L1\nL2\nL3\nL4\nL5\nL6\n"
    inserted = "\n".join(f"N{i}" for i in range(1, 11))
    plan = plan_file_mutations(
        {"sample.txt": original},
        [
            {"type": "insert", "path": "sample.txt", "line": 2, "content": inserted},
            {"type": "replace_line", "path": "sample.txt", "line": 5, "content": "TARGET"},
        ],
    )["sample.txt"]

    lines = plan.after.splitlines()
    assert lines[0] == "L1"
    assert lines[1:11] == [f"N{i}" for i in range(1, 11)]
    assert lines[11:14] == ["L2", "L3", "L4"]
    assert lines[14] == "TARGET"
    assert lines[15] == "L6"


def test_same_file_overlapping_original_snapshot_edits_fail_closed():
    with pytest.raises(SafeEditError) as exc:
        plan_file_mutations(
            {"sample.txt": "one\ntwo\nthree\n"},
            [
                {"type": "replace_range", "path": "sample.txt", "start": 1, "end": 2, "content": "x"},
                {"type": "replace_line", "path": "sample.txt", "line": 2, "content": "y"},
            ],
        )
    assert exc.value.code == "PATCH_OVERLAP"


@pytest.mark.parametrize(
    "source",
    [
        """
using System.Threading.Tasks;
public record ValueResult<T>(T Value);
public sealed class X {
    public Task<ValueResult<string>> Run() =>
        Task.FromException<ValueResult<string>>(new System.Exception("boom"));
}
""",
        """
public sealed class RawStrings {
    public string Json() => \"\"\"
        { "name": "value", "text": "this } is data" }
        \"\"\";
}
""",
        """
public sealed class Interpolation {
    public string Render(int value) => $"value={value}, literal={{ok}}";
}
""",
        """
public sealed class RawInterpolation {
    public string Render(int value) => $$\"\"\"
        { "value": {{value}}, "literal": "{ not code }" }
        \"\"\";
}
""",
        """
public sealed class LambdaCase {
    public Func<int, int> Build() => x => x is > 0 ? x + 1 : 0;
}
""",
        """
public record Item<T>(T? Value) where T : class, new();
""",
        """
[System.Obsolete("text with } and ) and ]")]
public sealed class Attributes {
    public string? Value { get; init; }
}
""",
    ],
)
def test_valid_csharp_modern_syntax_is_not_rejected_by_structural_precheck(source):
    validate_text("Sample.cs", source)


def test_csharp_precheck_still_rejects_unambiguous_unbalanced_code_delimiter():
    with pytest.raises(SafeEditError) as exc:
        validate_text("Broken.cs", "public class Broken { public void M() { if (true) { } }")
    assert exc.value.code == "STRUCTURAL_VALIDATION_FAILED"


def test_sequenced_mutation_persists_progressive_receipt_states(tmp_path, monkeypatch):
    import json
    from easychange.core.command_service import CommandService
    from easychange.core.workspace import Workspace

    target = tmp_path / "receipt-progress.txt"
    target.write_text("before\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    observed = []
    original_update = service.result_store.update_mutation_state

    def recording_update(sequence, lifecycle_state, *, mutation_receipt=None):
        if isinstance(mutation_receipt, dict):
            observed.append(dict(mutation_receipt))
        return original_update(
            sequence,
            lifecycle_state,
            mutation_receipt=mutation_receipt,
        )

    monkeypatch.setattr(service.result_store, "update_mutation_state", recording_update)
    payload = {
        "op": "operation",
        "type": "write_file",
        "path": "receipt-progress.txt",
        "content": "after\n",
    }
    result = service.execute(":ec QPROGRESS1 :j1 " + json.dumps(payload))
    assert result.ok, result.to_dict()

    states = [item.get("lifecycle_state") for item in observed]
    assert states[0] == "NOT_STARTED"
    assert "STARTED" in states
    assert "APPLIED" in states

    receipt = result.data["mutation_receipt"]
    assert receipt["state"] == "COMMITTED"  # legacy compatibility
    assert receipt["lifecycle_state"] in {"APPLIED", "VALIDATED"}
    assert receipt["sequence"] == "QPROGRESS1"
    assert receipt["operations_requested"] == 1
    assert receipt["operations_applied"] == 1
    assert receipt["matched_occurrences"] == 1
    assert receipt["journal_ids"]
    assert receipt["files"][0]["before_hash"]
    assert receipt["files"][0]["after_hash"]

    durable = service.result_store.status("QPROGRESS1")
    assert durable["state"] == "DONE"
    assert durable["mutation_receipt"]["lifecycle_state"] in {"APPLIED", "VALIDATED"}
    service.close()


def test_invalid_sequenced_mutation_finishes_with_failed_receipt(tmp_path):
    import json
    from easychange.core.command_service import CommandService
    from easychange.core.workspace import Workspace

    service = CommandService(Workspace.open(tmp_path))
    payload = {
        "op": "operation",
        "type": "replace_line",
        "path": "missing.txt",
        "line": 1,
        "content": "x",
    }
    result = service.execute(":ec QFAILED1 :j1 " + json.dumps(payload))
    assert result.ok is False
    receipt = result.data["mutation_receipt"]
    assert receipt["state"] == "FAILED"
    assert receipt["lifecycle_state"] == "FAILED"
    assert receipt["sequence"] == "QFAILED1"
    durable = service.result_store.status("QFAILED1")
    assert durable["mutation_receipt"]["lifecycle_state"] == "FAILED"
    service.close()
