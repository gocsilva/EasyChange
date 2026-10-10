"""The per-module OS inbox lease must be a regular file, not a redirected path."""
from __future__ import annotations

import os
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import module_bridge


def test_lease_rejects_nonregular_object_and_closes_its_descriptor(tmp_path):
    lease_path = tmp_path / "consumer.lock"
    opened = []
    real_open = os.open

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    with patch("module_bridge._consumer_lease_path", return_value=str(lease_path)), \
         patch("module_bridge.os.open", side_effect=tracked_open), \
         patch("module_bridge.os.fstat",
               return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)):
        with pytest.raises(RuntimeError, match="MCP_CONSUMER_LEASE_UNSAFE_TYPE"):
            module_bridge._acquire_consumer_lease()

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_consumer_lease_refuses_symlink_before_open(tmp_path):
    target = tmp_path / "other-file"
    target.write_bytes(b"untouched")
    alias = tmp_path / "consumer.lock"
    try:
        alias.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("This host cannot create symlinks")

    with patch("module_bridge._consumer_lease_path", return_value=str(alias)), \
         patch("module_bridge.os.open") as opened:
        with pytest.raises(RuntimeError, match="MCP_CONSUMER_LEASE_UNSAFE_PATH"):
            module_bridge._acquire_consumer_lease()
    opened.assert_not_called()
    assert target.read_bytes() == b"untouched"


def test_regular_lease_still_releases_and_can_reacquire(tmp_path):
    path = tmp_path / "consumer.lock"
    with patch("module_bridge._consumer_lease_path", return_value=str(path)):
        first = module_bridge._acquire_consumer_lease()
        try:
            assert stat.S_ISREG(os.fstat(first.fileno()).st_mode)
        finally:
            module_bridge._release_consumer_lease(first)
        second = module_bridge._acquire_consumer_lease()
        module_bridge._release_consumer_lease(second)
