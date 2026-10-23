from pathlib import Path


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
