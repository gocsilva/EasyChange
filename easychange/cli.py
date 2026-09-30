from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from .core.command_service import CommandService
from .core.workspace import Workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easychange", description="Machine-first development workspace")
    parser.add_argument("args", nargs="*", help="[workspace] or a one-shot command and its arguments")
    parser.add_argument("--workspace", dest="workspace_option", help="Workspace directory for a one-shot command")
    parser.add_argument("--json", action="store_true", help="Emit JSON results")
    options = parser.parse_args(argv)
    try:
        command_names = set(CommandService(Workspace.open(Path(options.workspace_option or "."))).execute(":capabilities").data["commands"])
        one_shot = bool(options.args and options.args[0].lstrip(":") in command_names)
        if options.workspace_option:
            workspace_path = options.workspace_option
        elif one_shot:
            workspace_path = "."
        else:
            workspace_path = options.args[0] if options.args else "."
        service = CommandService(Workspace.open(Path(workspace_path)))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if options.json:
        service.output = "json"
    if one_shot:
        command = options.args[0] + (" " + shlex.join(options.args[1:]) if len(options.args) > 1 else "")
        result = service.execute(command)
        print(result.render(service.output))
        return 0 if result.ok else 1
    print(service.execute(":state").render(service.output))
    try:
        while True:
            try:
                line = input("COMMAND > ")
            except EOFError:
                break
            result = service.execute(line)
            print(result.render(service.output))
            if result.data.get("quit"):
                break
    except KeyboardInterrupt:
        print("\nOK: closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
