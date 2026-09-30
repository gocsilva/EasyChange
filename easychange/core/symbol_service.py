from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .indexer import Indexer
from .workspace import Workspace


@dataclass(slots=True)
class Symbol:
    id: str
    name: str
    kind: str
    file: str
    line: int
    column: int
    signature: str
    adapter: str = "generic-regex"


class SymbolProvider(Protocol):
    def symbols(self, relative_path: str, content: str) -> list[Symbol]: ...


class RegexSymbolProvider:
    _patterns = (
        ("python", re.compile(r"^\s*(async\s+def|def)\s+(\w+)\s*\((.*)\)\s*:?")),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\((.*)\)")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|async|virtual|override|\s)*\s*[\w<>?\[\].,]+\s+(\w+)\s*\(([^;]*)\)\s*\{?\s*$")),
    )

    def symbols(self, relative_path: str, content: str) -> list[Symbol]:
        result = []
        suffix = Path(relative_path).suffix.casefold()
        for line_number, line in enumerate(content.splitlines(), 1):
            name = kind = ""
            signature = line.strip()
            if suffix == ".py":
                match = self._patterns[0][1].match(line)
                if match:
                    kind = "function"; name = match.group(2)
            if not name:
                for pattern_kind, pattern in self._patterns[1:]:
                    match = pattern.match(line)
                    if match:
                        kind = pattern_kind; name = match.group(1); break
            if name:
                result.append(Symbol(f"S{len(result)+1}", name, kind, relative_path, line_number,
                                     max(1, len(line) - len(line.lstrip()) + 1), signature))
        return result


class SymbolService:
    def __init__(self, workspace: Workspace, indexer: Indexer, providers: list[SymbolProvider] | None = None) -> None:
        self.workspace = workspace
        self.indexer = indexer
        self.providers = providers or [RegexSymbolProvider()]

    def symbols(self, path: str | None = None) -> list[dict]:
        indexed = self.indexer.files(limit=100000)
        rows = [item for item in indexed if path is None or item["path"] == path]
        output = []
        for item in rows:
            if item["size"] > 2 * 1024 * 1024:
                continue
            file_path = self.workspace.resolve(item["path"], must_exist=True)
            try: content = file_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError): continue
            for provider in self.providers:
                for symbol in provider.symbols(item["path"], content):
                    symbol.id = f"S{len(output)+1}"
                    output.append(asdict(symbol))
        return output

    def definition(self, name: str) -> list[dict]:
        return [item for item in self.symbols() if item["name"] == name]

    def references(self, name: str, limit: int = 100) -> list[dict]:
        return self.indexer.search(rf"\b{re.escape(name)}\b", regex=True, limit=limit)
