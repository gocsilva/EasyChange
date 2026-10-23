import subprocess

from easychange.core.git_service import GitService
from easychange.core.workspace import Workspace


def test_git_status_unborn_branch_reports_actual_branch(tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    service = GitService(Workspace(root_path=repo, name="fresh"))
    result = service.status(summary=True)
    assert result["branch"] == "main"
    assert result["clean"] is True
    assert result["change_count"] == 0
