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
        except RuntimeError as exc:
            if str(exc) == "HUB_ATTACH_SUPERSEDED":
                results.append({"superseded":True})
            else:
                failures.append(exc)
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
         patch.object(bridge,"_runtime_token",candidate), \
         patch.object(bridge,"_runtime_detached",False):
        a=threading.Thread(target=attaching,daemon=True)
        b=threading.Thread(target=detaching,daemon=True)
        a.start()
        try:
            assert entered.wait(timeout=3), "attach did not begin"
            b.start()
            assert detach_attempted.wait(timeout=3)
            # Revocation must not wait for the network verification.
            assert finished.wait(timeout=.5)
            assert results==[{"connected":False}]
        finally:
            release.set()
        a.join(timeout=3)
        b.join(timeout=3)
        assert not a.is_alive() and not b.is_alive()
        assert not failures, failures
        assert results == [{"connected":False},{"superseded":True}]
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


def test_implicit_client_sees_atomic_revoked_state_not_inherited_token():
    inherited="i"*48
    started=threading.Event()
    finished=threading.Event()
    result=[]

    def make_client():
        started.set()
        result.append(bridge.MCPBridge().token)
        finished.set()

    with patch.object(bridge,"_runtime_token",inherited), \
         patch.object(bridge,"_runtime_detached",False), \
         patch.dict(bridge.os.environ,{"MCP_MODULE_BUS_TOKEN":inherited}):
        with bridge._runtime_state_lock:
            thread=threading.Thread(target=make_client,daemon=True)
            thread.start()
            assert started.wait(timeout=3)
            # A consistent snapshot must wait for this transition's lock.
            assert not finished.wait(timeout=.05)
            bridge._runtime_token=""
            bridge._runtime_detached=True
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert result==[""]


def test_implicit_client_connection_check_observes_revocation_atomically():
    credential="c"*48
    started=threading.Event()
    finished=threading.Event()
    result=[]

    with patch.object(bridge,"_runtime_token",credential), \
         patch.object(bridge,"_runtime_detached",False):
        client=bridge.MCPBridge()

        def check():
            started.set()
            result.append(client.connected)
            finished.set()

        with bridge._runtime_state_lock:
            thread=threading.Thread(target=check,daemon=True)
            thread.start()
            assert started.wait(timeout=3)
            assert not finished.wait(timeout=.05)
            bridge._runtime_token=""
            bridge._runtime_detached=True
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert result==[False]
