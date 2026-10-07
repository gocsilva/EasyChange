from __future__ import annotations

import inspect
import os
import subprocess

import pytest

from easychange.core.process_service import ProcessService
from easychange.core.subprocess_utils import hidden_console_kwargs


@pytest.mark.skipif(os.name != "nt", reason="Windows console visibility contract")
def test_hidden_console_kwargs_prevent_console_flashes_on_windows():
    kwargs = hidden_console_kwargs()
    assert int(kwargs.get("creationflags") or 0) & int(subprocess.CREATE_NO_WINDOW)
    startupinfo = kwargs.get("startupinfo")
    assert startupinfo is not None
    assert int(startupinfo.dwFlags) & int(subprocess.STARTF_USESHOWWINDOW)
    assert int(startupinfo.wShowWindow) == 0


def test_process_start_is_headless_by_default():
    signature = inspect.signature(ProcessService.start)
    assert signature.parameters["no_window"].default is True
