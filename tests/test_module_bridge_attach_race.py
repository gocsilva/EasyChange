"""Attach/detach authorization must be linearizable across HTTP handlers."""
from __future__ import annotations

import threading
from unittest.mock import patch

import module_bridge as bridge


def test_delayed_attach_cannot_reinstall_bearer_after_concurrent_detach():
    candidate="a"*48
    entered=threading.Event()
    release=threading.Event()
    detach_attempted=threading.Event()
    finished=threading.Event()
    failures=[]
    results=[]

    def blocked_peers(_self):
        entered.set()
        assert release.wait(timeout=3), "attach verification never resumed"
        return {"ok":True,"self":bridge.CANONICAL_MODULE_ID}

    def attaching():
        try:
            results.append(bridge.attach_hub(candidate,bridge.CANONICAL_MODULE_ID))
        except BaseException as exc:
            failures.append(exc)

    def detaching():
        detach_attempted.set()
        try:
            results.append(bridge.detach_hub(candidate))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    with patch.object(bridge.MCPBridge,"peers",blocked_peers), \
         patch.object(bridge,"_runtime_token",""), \
         patch.object(bridge,"_runtime_detached",True):
        a=threading.Thread(target=attaching,daemon=True)
        b=threading.Thread(target=detaching,daemon=True)
        a.start()
        try:
            assert entered.wait(timeout=3), "attach did not begin"
            b.start()
            assert detach_attempted.wait(timeout=3)
            # Detach must be queued behind the outstanding verification.
            assert not finished.wait(timeout=.05)
        finally:
            release.set()
        a.join(timeout=3)
        b.join(timeout=3)
        assert not a.is_alive() and not b.is_alive()
        assert not failures, failures
        assert results == [
            {"connected":True,"id":bridge.CANONICAL_MODULE_ID},
            {"connected":False},
        ]
        assert bridge._runtime_token==""
        assert bridge._runtime_detached is True
        # Already-instantiated implicit bridges must remain revoked.
        assert bridge.MCPBridge().connected is False


def test_failed_attach_verification_cannot_replace_existing_credential():
    existing="x"*48
    candidate="y"*48
    with patch.object(bridge,"_runtime_token",existing), \
         patch.object(bridge,"_runtime_detached",False), \
         patch.object(bridge.MCPBridge,"peers",
                      return_value={"ok":True,"self":"wrong-module"}):
        try:
            bridge.attach_hub(candidate,bridge.CANONICAL_MODULE_ID)
        except PermissionError as exc:
            assert "HUB_MODULE_IDENTITY_MISMATCH" in str(exc)
        else:
            raise AssertionError("mismatched hub was allowed")
        assert bridge._runtime_token==existing
        assert bridge._runtime_detached is False
