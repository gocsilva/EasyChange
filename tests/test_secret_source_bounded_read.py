"""A workspace secret file can change between stat() and read()."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def test_secret_read_is_bounded_even_if_file_grows_after_stat(tmp_path, monkeypatch):
    source=tmp_path/"secret.env"
    source.write_text("tiny", encoding="utf-8")
    service=CommandService(Workspace.open(tmp_path))
    original_open=Path.open
    reads=[]

    class GrowingSecret(io.BytesIO):
        def read(self, count=-1):
            reads.append(count)
            return super().read(count)

    def open_with_concurrent_growth(path, mode="r", *args, **kwargs):
        if path==source and mode=="rb":
            return GrowingSecret(b"S"*(64*1024+2))
        return original_open(path,mode,*args,**kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", open_with_concurrent_growth)
            with pytest.raises(ValueError,match="SECRET_SOURCE_FILE_TOO_LARGE"):
                service._secret_value_from_workspace_source("secret.env")
        assert 64*1024+1 in reads
        assert -1 not in reads
        assert service.secret_broker.status()["count"]==0
    finally:
        service.close()


def test_small_secret_source_still_imports_under_existing_budget(tmp_path):
    source=tmp_path/"secret.txt"
    source.write_text("opaque-value",encoding="utf-8")
    service=CommandService(Workspace.open(tmp_path))
    try:
        value, metadata=service._secret_value_from_workspace_source("secret.txt")
        assert value=="opaque-value"
        assert isinstance(metadata,dict)
    finally:
        service.close()
