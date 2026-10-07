from __future__ import annotations

import os
import subprocess


def hidden_console_kwargs() -> dict:
    """Return subprocess kwargs that prevent console-window flashes on Windows.

    EasyChange is a GUI application. Console children such as git, rg, cmd.exe,
    dotnet, tasklist, database CLIs, and wsl.exe must run headlessly unless a
    caller explicitly launches a GUI program such as Edge/Chrome.
    """
    if os.name != "nt":
        return {}

    kwargs: dict = {}
    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window

    startup_cls = getattr(subprocess, "STARTUPINFO", None)
    use_show_window = int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
    if startup_cls is not None and use_show_window:
        startupinfo = startup_cls()
        startupinfo.dwFlags |= use_show_window
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo

    return kwargs
