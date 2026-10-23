import sqlite3

from easychange.core.database_service import DatabaseService
from easychange.core.workspace import Workspace


def test_sqlite_query_clamps_row_limit_before_paging(tmp_path):
    db = tmp_path / "rows.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE rows (n INTEGER)")
        connection.executemany(
            "INSERT INTO rows (n) VALUES (?)",
            ((i,) for i in range(5002)),
        )
    service = DatabaseService(Workspace(root_path=tmp_path, name="db"))

    minimum = service.query("rows.sqlite", "SELECT n FROM rows ORDER BY n", max_rows=0)
    assert minimum["max_rows"] == 1
    assert minimum["row_count"] == 1
    assert minimum["truncated"] is True
    assert minimum["has_more"] is True

    maximum = service.query("rows.sqlite", "SELECT n FROM rows ORDER BY n", max_rows=5001)
    assert maximum["max_rows"] == 5000
    assert maximum["row_count"] == 5000
    assert maximum["truncated"] is True
    assert maximum["has_more"] is True
