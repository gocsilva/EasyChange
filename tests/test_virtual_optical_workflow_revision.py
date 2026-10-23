from pathlib import Path
import re


def test_easychange_workflow_uses_one_revision_for_source_and_lab():
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "virtual-optical-lab.yml"
    ).read_text(encoding="utf-8")

    assert (
        'if [[ "$KIND" == "easychange" ]]; then '
        'LAB_SOURCE_DIR=component; else LAB_SOURCE_DIR=easychange; fi'
    ) in workflow
    assert 'python -m pip install -e "./$LAB_SOURCE_DIR[gui,dev]"' in workflow
    assert (
        'python -m pytest -q '
        '"$GITHUB_WORKSPACE/$LAB_SOURCE_DIR/tests/test_virtual_optical_contract.py"'
    ) in workflow
    assert (
        'python "$GITHUB_WORKSPACE/$LAB_SOURCE_DIR/tests/virtual_optical_lab.py"'
    ) in workflow
    assert '--easychange-source "$GITHUB_WORKSPACE/$LAB_SOURCE_DIR"' in workflow


def test_optical_workflow_actions_are_immutable_and_dependabot_updates_them():
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "virtual-optical-lab.yml").read_text(
        encoding="utf-8"
    )
    action_lines = [
        line.strip()
        for line in workflow.splitlines()
        if "uses: actions/" in line
    ]

    assert len(action_lines) == 4
    assert all(
        re.fullmatch(r"(?:- )?uses: actions/[a-z-]+@[0-9a-f]{40}(?: # v\d+\.\d+\.\d+)?", line)
        for line in action_lines
    )

    dependabot = (root / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: github-actions" in dependabot
    assert "directory: /" in dependabot
    assert "interval: monthly" in dependabot
