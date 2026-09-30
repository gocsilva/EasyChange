from __future__ import annotations

import re
from typing import Protocol

from .indexer import Indexer
from .workspace import Workspace


class SymbolProvider(Protocol):
    def symbols(self, relative_path: str, content: str) -> list[object]: ...


class SymbolService:
    """Thin symbol facade backed by the persistent SQLite index.

    Symbol extraction now happens while file content is already in memory during
    indexing, so definition/study queries do not reopen every source file.
    """

    def __init__(self, workspace: Workspace, indexer: Indexer, providers: list[SymbolProvider] | None = None) -> None:
        self.workspace = workspace
        self.indexer = indexer
        self.providers = providers or []

    def symbols(self, path: str | None = None) -> list[dict]:
        return self.indexer.symbols(path=path)

    def definition(self, name: str) -> list[dict]:
        return self.indexer.symbols(name=name)

    def references(self, name: str, limit: int = 100) -> list[dict]:
        return self.indexer.search(rf"\b{re.escape(name)}\b", regex=True, limit=limit)
