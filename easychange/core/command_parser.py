from __future__ import annotations

DEFAULT_ALIASES = {"o": "open", "r": "read", "s": "search", "b": "build", "t": "test", "d": "diff"}


class CommandParser:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.aliases = dict(DEFAULT_ALIASES)
        if aliases:
            self.aliases.update(aliases)

    @staticmethod
    def _tokenize(raw: str) -> list[str]:
        """Tokenize command text while preserving Windows backslashes.

        Backslashes are ordinary characters except immediately before the
        active quote delimiter, where one-or-more slashes represent a literal
        quote from transport/JSON escaping. This keeps Windows paths intact
        while supporting escaped quotes inside arguments.
        """
        tokens: list[str] = []
        token: list[str] = []
        quote: str | None = None
        explicit_empty = False
        index = 0

        def flush() -> None:
            nonlocal explicit_empty
            if token or explicit_empty:
                tokens.append("".join(token))
                token.clear()
                explicit_empty = False

        while index < len(raw):
            char = raw[index]

            if quote is not None:
                if char == quote:
                    quote = None
                    index += 1
                    continue
                if char == "\\":
                    end = index
                    while end < len(raw) and raw[end] == "\\":
                        end += 1
                    if end < len(raw) and raw[end] == quote:
                        token.append(quote)
                        index = end + 1
                        continue
                    token.extend(raw[index:end])
                    index = end
                    continue
                token.append(char)
                index += 1
                continue

            if char.isspace():
                flush()
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                explicit_empty = True
                index += 1
                continue
            if char == "\\":
                end = index
                while end < len(raw) and raw[end] == "\\":
                    end += 1
                if end < len(raw) and raw[end] in {"'", '"'}:
                    token.append(raw[end])
                    index = end + 1
                    continue
                token.extend(raw[index:end])
                index = end
                continue
            token.append(char)
            index += 1

        if quote is not None:
            raise ValueError("Unclosed quote")
        flush()
        return tokens

    def parse(self, text: str) -> list[str]:
        raw = text.strip()
        if raw.startswith(":"):
            raw = raw[1:]

        tokens: list[str] = []
        token: list[str] = []
        quote: str | None = None
        escaped_boundary = False
        explicit_empty = False
        index = 0

        def flush() -> None:
            nonlocal explicit_empty
            if token or explicit_empty:
                tokens.append("".join(token))
                token.clear()
                explicit_empty = False

        while index < len(raw):
            char = raw[index]

            if quote is None:
                if char.isspace():
                    flush()
                    index += 1
                    continue
                if char in {"'", '"'}:
                    quote = char
                    escaped_boundary = False
                    explicit_empty = True
                    index += 1
                    continue
                # Accept command text arriving with escaped outer quotes,
                # e.g. \"C:\Program Files\app.exe\". Backslashes in
                # ordinary Windows paths are otherwise preserved verbatim.
                if (
                    char == "\\"
                    and index + 1 < len(raw)
                    and raw[index + 1] in {"'", '"'}
                    and not token
                ):
                    quote = raw[index + 1]
                    escaped_boundary = True
                    explicit_empty = True
                    index += 2
                    continue
                token.append(char)
                index += 1
                continue

            # Inside a quoted token.
            if char == quote and not escaped_boundary:
                quote = None
                index += 1
                continue

            if char == "\\":
                run_start = index
                while index < len(raw) and raw[index] == "\\":
                    index += 1
                slash_count = index - run_start
                if index < len(raw) and raw[index] == quote:
                    next_index = index + 1
                    next_is_boundary = (
                        next_index >= len(raw) or raw[next_index].isspace()
                    )
                    if escaped_boundary and slash_count == 1 and next_is_boundary:
                        quote = None
                        escaped_boundary = False
                        index += 1
                        continue
                    # Escaped quote inside a quoted token. Command text often
                    # traverses JSON/HID before reaching this parser, so any
                    # backslash run used solely to escape the quote is syntax,
                    # not part of the final argument.
                    token.append(quote)
                    index += 1
                    continue
                token.extend("\\" * slash_count)
                continue

            if escaped_boundary and char == quote:
                # A bare quote inside an escaped-boundary token is literal;
                # the matching escaped quote closes it.
                token.append(char)
                index += 1
                continue

            token.append(char)
            index += 1

        if quote is not None:
            raise ValueError("Unclosed quote")
        flush()
        if tokens and tokens[0].lower() in self.aliases:
            tokens[0] = self.aliases[tokens[0].lower()]
        return tokens

    def split_chain(self, text: str) -> list[tuple[str, bool]]:
        """Return command/stop_on_failure pairs; semicolon continues and && stops."""
        parts: list[tuple[str, bool]] = []
        quote: str | None = None
        escaped_boundary = False
        token: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\\":
                end = index
                while end < len(text) and text[end] == "\\":
                    end += 1
                if end < len(text) and text[end] in {"'", '"'}:
                    delimiter = text[end]
                    next_index = end + 1
                    next_boundary = (
                        next_index >= len(text)
                        or text[next_index].isspace()
                        or text[next_index] == ";"
                        or text.startswith("&&", next_index)
                    )
                    # The EC1 parser accepts JSON/HID-escaped outer quotes.
                    # Track their boundaries here as well, otherwise a
                    # semicolon or && in a Windows path splits the command,
                    # and a closing \" appears to be an escaped literal.
                    if (quote is None and end == index + 1
                            and (not token or token[-1].isspace())):
                        quote = delimiter
                        escaped_boundary = True
                    elif (quote == delimiter and escaped_boundary
                          and end == index + 1 and next_boundary):
                        quote = None
                        escaped_boundary = False
                    token.extend(text[index:end])
                    token.append(delimiter)
                    index = end + 1
                    continue
                token.extend(text[index:end])
                index = end
                continue
            if char in {"'", '"'}:
                if quote == char:
                    quote = None
                    escaped_boundary = False
                elif quote is None:
                    quote = char
                    escaped_boundary = False
                token.append(char)
                index += 1
                continue
            if quote is None and char == ";":
                if "".join(token).strip():
                    parts.append(("".join(token).strip(), False))
                token = []
                index += 1
                continue
            if quote is None and text.startswith("&&", index):
                if "".join(token).strip():
                    parts.append(("".join(token).strip(), True))
                token = []
                index += 2
                continue
            token.append(char)
            index += 1
        if quote is not None:
            raise ValueError("Unclosed quote")
        if "".join(token).strip():
            parts.append(("".join(token).strip(), False))
        return parts
