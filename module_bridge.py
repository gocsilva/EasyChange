"""Optional MCP hub client; independent process can join the authenticated MCP host."""
from __future__ import annotations
import errno
import hashlib
import tempfile
import hmac
import threading
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request,build_opener,HTTPRedirectHandler,ProxyHandler

BUS=os.environ.get("MCP_MODULE_BUS_URL","http://127.0.0.1:8786")
if BUS not in ("http://127.0.0.1:8786","http://localhost:8786"):
    raise ValueError("MCP_BUS_MUST_USE_LOOPBACK")

class _NoRedirect(HTTPRedirectHandler):
    """Never forward MCP credentials to redirect destinations."""
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None

# The MCP hub must never be reached through an external HTTP proxy.
_local_opener=build_opener(ProxyHandler({}),_NoRedirect)

_runtime_token=""
# Single consumer in this module process, including multiple MCPBridge objects.
_processing_lock=threading.Lock()


def _acquire_consumer_lease():
    """One consumer per canonical module across all local processes.

    Keep the OS lock file in place: unlinking it while leased creates a
    second-inode race. A crashed process releases its file descriptor.
    """
    key=hashlib.sha256(
        os.path.normcase(os.path.abspath(__file__)).encode("utf-8")
    ).hexdigest()[:24]
    path=os.path.join(tempfile.gettempdir(),"mcp-module-inbox-"+key+".lock")
    flags=os.O_CREAT|os.O_RDWR|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0)
    try:
        fd=os.open(path,flags,0o600)
    except OSError as exc:
        raise RuntimeError("MCP_CONSUMER_LEASE_UNAVAILABLE") from exc
    lease=os.fdopen(fd,"r+b",buffering=0)
    try:
        if os.name=="nt":
            import msvcrt
            if os.fstat(lease.fileno()).st_size==0:
                lease.write(b"0")
            lease.seek(0)
            msvcrt.locking(lease.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return lease
    except OSError as exc:
        lease.close()
        if exc.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):
            raise RuntimeError("MCP_CONSUMER_BUSY") from exc
        raise RuntimeError("MCP_CONSUMER_LEASE_UNAVAILABLE") from exc


def _release_consumer_lease(lease):
    try:
        if os.name=="nt":
            import msvcrt
            lease.seek(0)
            msvcrt.locking(lease.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(lease.fileno(),fcntl.LOCK_UN)
    finally:
        lease.close()


class MCPBridge:
    def __init__(self,token=None):
        self.token=token if token is not None else (_runtime_token or os.environ.get("MCP_MODULE_BUS_TOKEN",""))
        self.module=os.environ.get("MCP_MODULE_ID","")

    @property
    def connected(self):
        return bool(self.token)

    def _request(self,path,body=None,*,timeout=40):
        if not self.token:raise RuntimeError("MCP_NOT_CONNECTED_STANDALONE")
        if path not in ("/v1/tools","/v1/tools/call","/v1/peers","/v1/send",
                        "/v1/inbox","/v1/inbox/peek","/v1/inbox/ack"):
            raise ValueError("MCP_BUS_PATH_NOT_ALLOWED")
        headers={"Authorization":"Bearer "+self.token}
        payload=None
        if body is not None:
            payload=json.dumps(body,ensure_ascii=False).encode("utf-8")
            headers["Content-Type"]="application/json"
        req=Request(BUS+path,data=payload,method="POST" if payload is not None else "GET",headers=headers)
        with _local_opener.open(req,timeout=timeout) as response:
            data=json.load(response)
        if not isinstance(data,dict):raise ValueError("INVALID_MCP_HUB_RESPONSE")
        return data

    def catalog(self):
        return self._request("/v1/tools")

    def call(self,tool,**arguments):
        return self._request("/v1/tools/call",{"tool":tool,"arguments":arguments},timeout=120)

    def peers(self):
        return self._request("/v1/peers",timeout=3)

    def send(self,target,topic,payload=None,correlation_id=""):
        message={"target":target,"topic":topic,
                 "payload":payload,"correlation_id":correlation_id}
        # A 429 guarantees this message was not accepted; bounded retries
        # avoid loss while preventing unbounded blocking when a peer is stuck.
        for attempt in range(7):
            try:
                return self._request("/v1/send",message,timeout=6)
            except HTTPError as exc:
                if exc.code!=429 or attempt==6:
                    raise
                time.sleep(min(0.5,0.025*(2**attempt)))

    def receive(self):
        """Non-destructive read; process and then explicitly call ack(ids)."""
        return self._request("/v1/inbox/peek")

    def ack(self, ids):
        """Confirm successful processing. Repeated ACKs are idempotent."""
        return self._request("/v1/inbox/ack",{"ids":ids})

    def process_pending(self, handler, *, limit=20, ack_batch_size=1):
        """Opt-in callback processing. ACK only after explicit True.

        The safe default ACKs each message individually. An idempotent handler
        may opt into bounded batch ACK for performance; a crash before batch
        commit can replay several already-executed operations. No side effects
        occur automatically when joining MCP. An OS lease also blocks a
        second local process from executing the same unacknowledged work.
        """
        if not callable(handler):
            raise ValueError("HANDLER_REQUIRED")
        if type(limit) is not int or not 1<=limit<=20:
            raise ValueError("INVALID_LIMIT")
        if type(ack_batch_size) is not int or not 1<=ack_batch_size<=20:
            raise ValueError("INVALID_ACK_BATCH_SIZE")
        if not self.connected:
            raise RuntimeError("MCP_NOT_CONNECTED_STANDALONE")
        if not _processing_lock.acquire(blocking=False):
            raise RuntimeError("MCP_CONSUMER_BUSY")
        lease=None
        try:
            lease=_acquire_consumer_lease()
            page=self.receive()
            if not isinstance(page,dict) or page.get("requires_ack") is not True:
                raise ValueError("MCP_REQUIRES_NONDESTRUCTIVE_INBOX")
            messages=page.get("messages")
            if not isinstance(messages,list):
                raise ValueError("INVALID_MCP_INBOX")
            selected=messages[:limit]
            unique=set()
            for message in selected:
                if (not isinstance(message,dict) or
                        type(message.get("id")) is not int or message["id"]<1):
                    raise ValueError("INVALID_MCP_INBOX_MESSAGE")
                if message["id"] in unique:
                    raise ValueError("DUPLICATE_MCP_INBOX_ID")
                unique.add(message["id"])
            validated_ids=tuple(message["id"] for message in selected)
            count=0
            accepted=[]
            def flush():
                nonlocal count
                if not accepted:
                    return
                receipt=self.ack(list(accepted))
                if not isinstance(receipt,dict) or receipt.get("ok") is not True:
                    raise RuntimeError("MCP_ACK_UNCONFIRMED")
                acked=receipt.get("acked")
                already=receipt.get("already_acked",[])
                unknown=receipt.get("unknown",[])
                if (type(acked) is not int or
                        not 0<=acked<=len(accepted) or
                        not isinstance(already,list) or
                        not isinstance(unknown,list) or unknown or
                        any(type(item) is not int or item not in accepted
                            for item in already) or
                        len(set(already))!=len(already) or
                        acked+len(already)!=len(accepted)):
                    raise RuntimeError("MCP_ACK_UNCONFIRMED")
                count+=len(accepted)
                accepted.clear()
            for message, original_id in zip(selected, validated_ids):
                try:
                    successful=handler(message) is True
                except Exception:
                    # Flush any prior successful operations, never the failing
                    # message. If ACK fails its uncertainty takes precedence.
                    flush()
                    raise
                if not successful:
                    flush()
                    return {"received":len(selected),"acknowledged":count,
                            "deferred_id":original_id}
                accepted.append(original_id)
                if len(accepted)>=ack_batch_size:
                    flush()
            flush()
            return {"received":len(selected),"acknowledged":count,"deferred_id":None}
        finally:
            try:
                if lease is not None:
                    _release_consumer_lease(lease)
            finally:
                _processing_lock.release()

    def receive_legacy(self):
        """Older destructive read for compatibility; avoid for reliable delivery."""
        return self._request("/v1/inbox")

def attach_hub(token,expected_module):
    """Join a pre-existing standalone service to MCP without killing its GUI.

    The proposed token must be accepted by the hub and authenticate as exactly
    this module. Only then is it installed; arbitrary localhost callers cannot
    claim a module or choose an external URL.
    """
    global _runtime_token
    if not isinstance(token,str) or len(token)<32 or len(token)>200:
        raise ValueError("INVALID_HUB_CREDENTIAL")
    members=MCPBridge(token).peers()
    if members.get("self")!=expected_module or members.get("ok") is not True:
        raise PermissionError("HUB_MODULE_IDENTITY_MISMATCH")
    _runtime_token=token
    return {"connected":True,"id":expected_module}

def detach_hub(token):
    global _runtime_token
    if not isinstance(token,str) or not _runtime_token or not hmac.compare_digest(token,_runtime_token):
        raise PermissionError("INVALID_DETACH_CREDENTIAL")
    _runtime_token=""
    return {"connected":False}

def self_test():
    return {"standalone":not bool(MCPBridge().token),
            "hub_optional":True,"tools_from_mcp":True}
