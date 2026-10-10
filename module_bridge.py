"""Optional MCP hub client; independent process can join the authenticated MCP host."""
from __future__ import annotations
import errno
import hashlib
import tempfile
import hmac
import threading
import json
import math
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
_runtime_detached=False
CANONICAL_MODULE_ID="easychange"
def _finite_hub_json_number(token):
    number=float(token)
    if not math.isfinite(number):
        raise ValueError("MCP_HUB_NONFINITE_JSON")
    return number

# Single consumer in this module process, including multiple MCPBridge objects.
_processing_lock=threading.Lock()


def _consumer_lease_path():
    """Stable per module, independent of checkout path or symlink aliases.

    The bus endpoint is fixed to local port 8786. Two copies of the same
    module must never acquire different consumer leases for its inbox.
    Other module identities retain independent leases.
    """
    key=hashlib.sha256(
        ("mcp-local-8786|"+CANONICAL_MODULE_ID).encode("ascii")
    ).hexdigest()[:24]
    return os.path.join(tempfile.gettempdir(),"mcp-module-inbox-"+key+".lock")


def _acquire_consumer_lease():
    """One consumer per canonical module across all local processes.

    Keep the OS lock file in place: unlinking it while leased creates a
    second-inode race. A crashed process releases its file descriptor.
    """
    path=_consumer_lease_path()
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
        # Explicit detach overrides an inherited bootstrap credential.
        # The host must also rotate its token to revoke pre-existing clients.
        self.token=(token if token is not None else
                    ("" if _runtime_detached else
                     (_runtime_token or os.environ.get("MCP_MODULE_BUS_TOKEN",""))))
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
            # Reject a single huge direct text field *before* json.dumps
            # copies and escapes it. This covers ordinary send payloads,
            # batched ACK IDs and top-level tool arguments. Nested structures
            # remain subject to the exact encoded-byte limit below.
            request_limit=(1_048_576 if path=="/v1/tools/call" else 16384+1024)
            if isinstance(body,dict):
                # Iterative scan with a bounded visit budget: deep, shared
                # or cyclic structures cannot make preflight loop forever.
                pending=[body]
                seen_containers=set()
                visits=0
                while pending:
                    item=pending.pop()
                    visits+=1
                    if visits>request_limit:
                        raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                    if isinstance(item,str):
                        if len(item)>request_limit:
                            raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                    elif isinstance(item,(dict,list,tuple)):
                        if id(item) in seen_containers:
                            continue
                        seen_containers.add(id(item))
                        if len(item)>request_limit:
                            raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                        if isinstance(item,dict):
                            pending.extend(item.keys())
                            pending.extend(item.values())
                        else:
                            pending.extend(item)
            payload=json.dumps(body,ensure_ascii=False,allow_nan=False).encode("utf-8")
            # The exact UTF-8 byte budget still decides final admission.
            if len(payload)>request_limit:
                raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
            headers["Content-Type"]="application/json"
        req=Request(BUS+path,data=payload,method="POST" if payload is not None else "GET",headers=headers)
        # The tool-RPC endpoint can legitimately return an 8 MiB result.
        # Metadata, mailbox, and ACK endpoints have much smaller envelopes;
        # avoid allocating 8 MiB per failed/noisy health or receipt probe.
        max_response = (
            8_388_608 + 131_072 if path == "/v1/tools/call"
            else 2_097_152 if path == "/v1/tools"
            else 1_048_576 if path in ("/v1/inbox", "/v1/inbox/peek")
            else 65_536
        )
        with _local_opener.open(req,timeout=timeout) as response:
            raw=response.read(max_response+1)
        if len(raw)>max_response:
            raise ValueError("MCP_HUB_RESPONSE_TOO_LARGE")
        data=json.loads(raw,parse_constant=_finite_hub_json_number,
                        parse_float=_finite_hub_json_number)
        if not isinstance(data,dict):raise ValueError("INVALID_MCP_HUB_RESPONSE")
        return data

    def catalog(self):
        return self._request("/v1/tools")

    def call(self,tool,**arguments):
        payload={"tool":tool,"arguments":arguments}
        # Retry only the hub's explicit *not executed* RPC-admission response.
        # A generic 429 or a transport timeout may follow real side effects,
        # so neither is safe to replay automatically.
        for attempt in range(7):
            try:
                return self._request("/v1/tools/call",payload,timeout=120)
            except HTTPError as exc:
                if exc.code!=429:
                    raise
                try:
                    details=json.loads(exc.read(2048))
                except (ValueError,UnicodeError):
                    raise exc
                if (not isinstance(details,dict) or
                        details.get("error")!="MODULE_TOOL_RPC_BUSY_RETRY_LATER" or
                        details.get("accepted") is not False or
                        details.get("retryable") is not True or attempt==6):
                    raise exc
                time.sleep(min(0.5,0.025*(2**attempt)))

    def peers(self):
        return self._request("/v1/peers",timeout=3)

    def send(self,target,topic,payload=None,correlation_id=""):
        message={"target":target,"topic":topic,
                 "payload":payload,"correlation_id":correlation_id}
        # A generic 429 does NOT guarantee the peer rejected an operation.
        # Replay only explicit mailbox-admission backpressure (accepted=False);
        # timeouts and responses with unknown outcomes remain non-retryable.
        for attempt in range(7):
            try:
                return self._request("/v1/send",message,timeout=6)
            except HTTPError as exc:
                if exc.code!=429:
                    raise
                try:
                    details=json.loads(exc.read(2048))
                except (ValueError,UnicodeError):
                    raise exc
                if (not isinstance(details,dict) or
                        details.get("error")!="MODULE_INBOX_FULL_RETRY_LATER" or
                        details.get("accepted") is not False or
                        details.get("retryable") is not True or attempt==6):
                    raise exc
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
            if ("recipient" in page and
                    page["recipient"]!=CANONICAL_MODULE_ID):
                raise ValueError("MCP_INBOX_RECIPIENT_MISMATCH")
            messages=page.get("messages")
            if not isinstance(messages,list):
                raise ValueError("INVALID_MCP_INBOX")
            selected=messages[:limit]
            unique=set()
            for message in selected:
                if (not isinstance(message,dict) or
                        type(message.get("id")) is not int or message["id"]<1):
                    raise ValueError("INVALID_MCP_INBOX_MESSAGE")
                if ("recipient" in page and
                        message.get("target") != CANONICAL_MODULE_ID):
                    raise ValueError("MCP_INBOX_TARGET_MISMATCH")
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
                if (not isinstance(receipt,dict) or receipt.get("ok") is not True or
                        ("recipient" in receipt and
                         receipt["recipient"]!=CANONICAL_MODULE_ID)):
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
                if "acked_ids" in receipt:
                    confirmed_ids=receipt["acked_ids"]
                    # New hubs provide exact identities, not only a count.
                    # Older hub receipts without acked_ids remain compatible.
                    if (not isinstance(confirmed_ids,list) or
                            any(type(item) is not int for item in confirmed_ids) or
                            len(set(confirmed_ids))!=len(confirmed_ids) or
                            len(confirmed_ids)!=acked or
                            set(confirmed_ids)&set(already) or
                            set(confirmed_ids)|set(already)!=set(accepted)):
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
    global _runtime_token, _runtime_detached
    if expected_module != CANONICAL_MODULE_ID:
        # A caller must never install another module's valid hub bearer on
        # this standalone process (cross-module identity confusion).
        raise PermissionError("HUB_MODULE_IDENTITY_MISMATCH")
    if not isinstance(token,str) or len(token)<32 or len(token)>200:
        raise ValueError("INVALID_HUB_CREDENTIAL")
    members=MCPBridge(token).peers()
    if members.get("self")!=expected_module or members.get("ok") is not True:
        raise PermissionError("HUB_MODULE_IDENTITY_MISMATCH")
    _runtime_token=token
    _runtime_detached=False
    return {"connected":True,"id":expected_module}

def detach_hub(token):
    global _runtime_token, _runtime_detached
    if not isinstance(token,str) or not _runtime_token or not hmac.compare_digest(token,_runtime_token):
        raise PermissionError("INVALID_DETACH_CREDENTIAL")
    _runtime_token=""
    _runtime_detached=True
    return {"connected":False}

def self_test():
    return {"standalone":not bool(MCPBridge().token),
            "hub_optional":True,"tools_from_mcp":True}
