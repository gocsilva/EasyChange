from __future__ import annotations

import shlex


DEFAULT_ALIASES = {"o": "open", "r": "read", "s": "search", "b": "build", "t": "test", "d": "diff"}


class CommandParser:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.aliases = dict(DEFAULT_ALIASES)
        if aliases: self.aliases.update(aliases)

    def parse(self, text: str) -> list[str]:
        raw = text.strip()
        if raw.startswith(":"): raw = raw[1:]
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.escape = ""
        tokens = list(lexer)
        if tokens and tokens[0].lower() in self.aliases:
            tokens[0] = self.aliases[tokens[0].lower()]
        return tokens

    def split_chain(self, text: str) -> list[tuple[str, bool]]:
        """Return (command, stop_on_failure); `;` continues and `&&` stops."""
        parts: list[tuple[str, bool]] = []
        quote: str | None = None
        token: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char in {"'", '"'}:
                if quote == char: quote = None
                elif quote is None: quote = char
                token.append(char); index += 1; continue
            if quote is None and char == ";":
                if "".join(token).strip(): parts.append(("".join(token).strip(), False))
                token = []; index += 1; continue
            if quote is None and text.startswith("&&", index):
                if "".join(token).strip(): parts.append(("".join(token).strip(), True))
                token = []; index += 2; continue
            token.append(char); index += 1
        if quote is not None: raise ValueError("Unclosed quote")
        if "".join(token).strip(): parts.append(("".join(token).strip(), False))
        return parts
