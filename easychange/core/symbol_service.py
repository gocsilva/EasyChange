from __future__ import annotations
import json

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
    _cs_modifiers = r"(?:(?:public|private|protected|internal|sealed|abstract|static|partial|new|readonly|ref|unsafe)\s+)*"
    _patterns = (
        ("python", re.compile(r"^\s*(async\s+def|def)\s+(\w+)\s*\((.*)\)\s*:?")),
        ("class", re.compile(rf"^\s*{_cs_modifiers}(?:export\s+)?class\s+(\w+)")),
        ("interface", re.compile(rf"^\s*{_cs_modifiers}(?:export\s+)?interface\s+(\w+)")),
        ("record", re.compile(rf"^\s*{_cs_modifiers}record(?:\s+class|\s+struct)?\s+(\w+)")),
        ("struct", re.compile(rf"^\s*{_cs_modifiers}struct\s+(\w+)")),
        ("enum", re.compile(rf"^\s*{_cs_modifiers}enum\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\((.*)\)")),
        ("method", re.compile(r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|new|\s)*\s*[\w<>?\[\].,]+\s+(\w+)\s*\(([^;]*)\)\s*\{?\s*$")),
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
        self.cache_path = workspace.root_path / ".easychange" / "symbols.json"
        self._cache: dict[str, dict] = {}
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}
            if isinstance(value, dict):
                self._cache = value
        except (OSError, ValueError, TypeError):
            self._cache = {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.cache_path)

    def symbols(self, path: str | None = None) -> list[dict]:
        indexed = self.indexer.files(limit=100000)
        rows = [item for item in indexed if path is None or item["path"] == path]
        output: list[dict] = []
        changed = False
        live_paths = {item["path"] for item in indexed}

        if path is None:
            stale = set(self._cache) - live_paths
            for item in stale:
                self._cache.pop(item, None)
                changed = True

        for item in rows:
            cached = self._cache.get(item["path"])
            if cached and cached.get("hash") == item["hash"]:
                symbols = list(cached.get("symbols") or [])
            else:
                if item["size"] > 2 * 1024 * 1024:
                    symbols = []
                else:
                    file_path = self.workspace.resolve(item["path"], must_exist=True)
                    try:
                        content = file_path.read_text(encoding="utf-8-sig")
                    except (OSError, UnicodeDecodeError):
                        content = ""
                    symbols = []
                    for provider in self.providers:
                        symbols.extend(asdict(symbol) for symbol in provider.symbols(item["path"], content))
                self._cache[item["path"]] = {"hash": item["hash"], "symbols": symbols}
                changed = True

            for symbol in symbols:
                value = dict(symbol)
                value["id"] = f"S{len(output) + 1}"
                output.append(value)

        if changed:
            self._save_cache()
        return output

    def definition(self, name: str) -> list[dict]:
        return [item for item in self.symbols() if item["name"] == name]

    def references(self, name: str, limit: int = 100) -> list[dict]:
        return self.indexer.search(rf"\b{re.escape(name)}\b", regex=True, limit=limit)
