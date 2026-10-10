"""SQL write permissions must be explicit booleans, never truthy strings."""
from __future__ import annotations

import pytest

from easychange.core.database_service import DatabaseService
from easychange.core.workspace import Workspace


@pytest.mark.parametrize("permission", (
    "false", "true", "0", "1", 0, 1, None, [], {}
))
def test_invalid_write_flag_fails_before_lookup_or_sql_execution(tmp_path, permission):
    db=DatabaseService(Workspace.open(tmp_path))

    def no_connection(*_args, **_kwargs):
        raise AssertionError("CONNECTION_LOOKUP_BEFORE_WRITE_PERMISSION_VALIDATION")
    db._connection=no_connection

    with pytest.raises(ValueError, match="ALLOW_WRITE_BOOLEAN_REQUIRED"):
        db.query("nonexistent", "DELETE FROM critical_table",
                 allow_write=permission)


def test_bool_write_flag_uses_normal_read_only_gate(tmp_path):
    db=DatabaseService(Workspace.open(tmp_path))
    with pytest.raises(PermissionError, match="WRITE_SQL_REQUIRES_ALLOW_WRITE"):
        db.query("nonexistent","DELETE FROM critical_table",allow_write=False)

    # A real True is the only value that gets beyond the write-permission
    # gate; it still needs a separately configured database connection.
    with pytest.raises(LookupError, match="Database connection not found"):
        db.query("nonexistent","DELETE FROM critical_table",allow_write=True)
