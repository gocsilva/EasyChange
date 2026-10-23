"""Invalid/empty search inputs must not scan and return unrelated source."""
import pytest

from easychange.core.search_service import SearchService
from easychange.core.workspace import Workspace


def test_empty_query_does_not_disclose_all_source_lines(tmp_path):
    (tmp_path/"private.cs").write_text("SECRET_VALUE\npublic method\n",encoding="utf-8")
    search=SearchService(Workspace(tmp_path,tmp_path.name))
    assert search.search("",limit=40)==[]
    assert search.search("",case_sensitive=True,limit=40)==[]


def test_nonstr_query_is_rejected_with_stable_error(tmp_path):
    (tmp_path/"private.cs").write_text("SECRET_VALUE\n",encoding="utf-8")
    search=SearchService(Workspace(tmp_path,tmp_path.name))
    with pytest.raises(ValueError,match="INVALID_SEARCH_QUERY"):
        search.search(None)
