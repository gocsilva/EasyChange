"""Optional MCP hub client; independent process can join the authenticated MCP host."""
from __future__ import annotations
import errno
import hashlib
import tempfile
import hmac
import threading
import json
import itertools
import math
import os
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request,build_opener,HTTPRedirectHandler,ProxyHandler

def _canonical_bus_url(value):
    # The core listens on IPv4 loopback. Accept the documented localhost
    # alias but never let OS name resolution select ::1 or an overridden host.
    if value in ("http://127.0.0.1:8786", "http://localhost:8786"):
        return "http://127.0.0.1:8786"
    raise ValueError("MCP_BUS_MUST_USE_LOOPBACK")

BUS=_canonical_bus_url(
    os.environ.get("MCP_MODULE_BUS_URL","http://127.0.0.1:8786"))

class _NoRedirect(HTTPRedirectHandler):
    """Never forward MCP credentials to redirect destinations."""
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None

# The MCP hub must never be reached through an external HTTP proxy.
_local_opener=build_opener(ProxyHandler({}),_NoRedirect)

_runtime_token=""
_runtime_detached=False
# HTTP module-service handlers may concurrently attach and detach the hub.
# Serialize credential transitions so a delayed attach cannot undo a detach.
_runtime_state_lock=threading.RLock()
CANONICAL_MODULE_ID="easychange"
def _finite_hub_json_number(token):
    number=float(token)
    if not math.isfinite(number):
        raise ValueError("MCP_HUB_NONFINITE_JSON")
    return number


def _hub_unique_fields(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError("MCP_HUB_DUPLICATE_JSON_KEY")
        result[key]=value
    return result


def _decode_hub_response(raw):
    # Ambiguous accepted/retryable fields must never authorize replay of a
    # potentially mutating call, even when received from a local TCP port.
    return json.loads(raw,parse_constant=_finite_hub_json_number,
                      parse_float=_finite_hub_json_number,
                      object_pairs_hook=_hub_unique_fields)

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
    # The predictable lock path is in the OS temp directory. Never acquire a
    # lock on a symlink, directory, FIFO or device supplied by another process.
    # O_NOFOLLOW closes the symlink race on platforms that provide it.
    if os.path.islink(path):
        raise RuntimeError("MCP_CONSUMER_LEASE_UNSAFE_PATH")
    flags=os.O_CREAT|os.O_RDWR|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0)
    try:
        fd=os.open(path,flags,0o600)
    except OSError as exc:
        raise RuntimeError("MCP_CONSUMER_LEASE_UNAVAILABLE") from exc
    try:
        metadata=os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("MCP_CONSUMER_LEASE_UNSAFE_TYPE")
        # A hard-linked predictable lock may alias another resource. A
        # single inode name is required for the per-module consumer lease.
        if os.name=="posix" and metadata.st_nlink != 1:
            raise RuntimeError("MCP_CONSUMER_LEASE_UNSAFE_LINKS")
        if os.name=="posix":
            # The shared temp filename is predictable. A pre-existing file
            # owned by another user or writable by peers must not become the
            # lock inode authorizing delivery of potentially mutating work.
            if metadata.st_uid!=os.getuid() or (metadata.st_mode & 0o022):
                raise RuntimeError("MCP_CONSUMER_LEASE_UNSAFE_OWNER")
        lease=os.fdopen(fd,"r+b",buffering=0)
    except BaseException:
        # The opened descriptor still belongs to this scope if fdopen fails
        # or the opened object is not a regular file.
        os.close(fd)
        raise
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
        self._implicit_credential=(token is None)
        # Take one consistent snapshot of both credential fields. In
        # particular, detach clears _runtime_token before setting the flag;
        # an unlocked reader could resurrect the inherited env token.
        if token is None:
            with _runtime_state_lock:
                token=("" if _runtime_detached else
                       (_runtime_token or os.environ.get("MCP_MODULE_BUS_TOKEN","")))
        self.token=token
        self.module=os.environ.get("MCP_MODULE_ID","")

    @property
    def connected(self):
        # Misconfigured inherited credentials must behave as disconnected,
        # rather than raising while constructing an HTTP Authorization header.
        if (type(self.token) is not str or not self.token or
                len(self.token)>256 or not self.token.isascii() or
                any(ord(char)<=32 or ord(char)==127 for char in self.token)):
            # A token that passes ASCII-only validation can still contain
            # spaces, CR or LF and fail during Authorization construction.
            # Treat malformed credentials as disconnected before network I/O.
            return False
        if self._implicit_credential:
            with _runtime_state_lock:
                # Implicit clients stop working immediately after a detach or
                # a newer attach. Explicit tokens are verified by the hub.
                if _runtime_detached:
                    return False
                if _runtime_token:
                    if (type(_runtime_token) is not str or
                            len(_runtime_token)>256 or
                            not _runtime_token.isascii() or
                            not hmac.compare_digest(self.token,_runtime_token)):
                        return False
        return True

    def _request(self,path,body=None,*,timeout=40):
        if not self.connected:raise RuntimeError("MCP_NOT_CONNECTED_STANDALONE")
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
                # Use iterator frames, not one temporary list reference
                # per member of a large RPC dictionary. This keeps scan
                # memory proportional to nesting depth.
                pending=[(iter((body,)),None)]
                active_containers=set()
                visits=0
                while pending:
                    iterator,owner=pending[-1]
                    try:
                        item=next(iterator)
                    except StopIteration:
                        pending.pop()
                        if owner is not None:
                            active_containers.remove(owner)
                        continue
                    visits+=1
                    if visits>request_limit:
                        raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                    if isinstance(item,str):
                        if len(item)>request_limit:
                            raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                    elif isinstance(item,(dict,list,tuple)):
                        identity=id(item)
                        if identity in active_containers:
                            # Let the JSON encoder reject a cycle. The
                            # preflight must not recurse forever.
                            continue
                        if len(item)>request_limit:
                            raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                        if isinstance(item,dict):
                            # Native nonstring keys can alias string keys in
                            # JSON; never send an ambiguous payload.
                            if any(type(key) is not str for key in item):
                                raise ValueError("MCP_BUS_INVALID_JSON_KEY")
                            children=itertools.chain(item.keys(),item.values())
                        else:
                            children=iter(item)
                        # Only ancestors belong in the active set. A global
                        # visited set grows with every unrelated child node.
                        active_containers.add(identity)
                        pending.append((children,identity))
            # Shared object graphs can expand exponentially in JSON.
            # json.dumps() materializes that expansion before a length check;
            # stream encoder fragments into a byte-limited buffer instead.
            encoded=bytearray()
            try:
                encoder=json.JSONEncoder(ensure_ascii=False,allow_nan=False)
                for chunk in encoder.iterencode(body):
                    if len(chunk)>request_limit:
                        raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                    # JSONEncoder can yield a huge single Unicode string.
                    # Enforce the byte budget before allocating its full
                    # UTF-8 expansion (up to 4x the source characters).
                    for offset in range(0,len(chunk),4096):
                        fragment=chunk[offset:offset+4096].encode("utf-8")
                        if len(encoded)+len(fragment)>request_limit:
                            raise ValueError("MCP_BUS_REQUEST_TOO_LARGE")
                        encoded.extend(fragment)
            except (RecursionError,OverflowError) as exc:
                raise ValueError("MCP_BUS_REQUEST_TOO_LARGE") from exc
            except UnicodeError as exc:
                # A lone surrogate cannot be sent as UTF-8 JSON. Keep this
                # a deterministic input error, never attempt network I/O.
                raise ValueError("MCP_BUS_INVALID_UNICODE") from exc
            payload=bytes(encoded)
            headers["Content-Type"]="application/json"
        req=Request(BUS+path,data=payload,method="POST" if payload is not None else "GET",headers=headers)
        # The tool-RPC endpoint can legitimately return an 8 MiB result.
        # Metadata, mailbox, and ACK endpoints have much smaller envelopes;
        # avoid allocating 8 MiB per failed/noisy health or receipt probe.
        max_response = (
            8_388_608 + 131_072 if path == "/v1/tools/call"
            else 2_097_152 if path == "/v1/tools"
            # The hub permits 100 messages of up to 16 KiB each (plus
            # envelope metadata). A 1 MiB inbox read limit rejects a valid
            # near-full batch even though the server admitted every message.
            else 2_097_152 if path in ("/v1/inbox", "/v1/inbox/peek")
            else 65_536
        )
        try:
            with _local_opener.open(req,timeout=timeout) as response:
                raw=response.read(max_response+1)
        except HTTPError as exc:
            # Informational GETs have no retry policy or reason to retain a
            # rejected response. Mutating endpoints keep their 429 response
            # until the caller verifies the explicit not-executed receipt.
            if path not in ("/v1/tools/call","/v1/send","/v1/inbox/ack"):
                exc.close()
            raise
        if len(raw)>max_response:
            raise ValueError("MCP_HUB_RESPONSE_TOO_LARGE")
        data=_decode_hub_response(raw)
        if not isinstance(data,dict):raise ValueError("INVALID_MCP_HUB_RESPONSE")
        return data

    def catalog(self):
        # Metadata-only localhost call: never spend a 40s tool-RPC budget on
        # a status screen. Actual tool calls retain their 120s timeout.
        return self._request("/v1/tools",timeout=8)

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
                    # Keep rejected requests from retaining HTTP sockets.
                    exc.close()
                    raise
                try:
                    # An admission-rejection proof must be complete: a
                    # truncated JSON prefix cannot authorize an operation
                    # replay when trailing bytes were not examined.
                    rejection=exc.read(2049)
                    if len(rejection)>2048:
                        raise exc
                    details=_decode_hub_response(rejection)
                except (ValueError,UnicodeError):
                    raise exc
                finally:
                    # Every acknowledged 429 is consumed by this retry path.
                    # Release its HTTP socket before the backoff delay rather
                    # than waiting for GC while the local hub is saturated.
                    exc.close()
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
                    # Keep rejected requests from retaining HTTP sockets.
                    exc.close()
                    raise
                try:
                    # An admission-rejection proof must be complete: a
                    # truncated JSON prefix cannot authorize an operation
                    # replay when trailing bytes were not examined.
                    rejection=exc.read(2049)
                    if len(rejection)>2048:
                        raise exc
                    details=_decode_hub_response(rejection)
                except (ValueError,UnicodeError):
                    raise exc
                finally:
                    # Every acknowledged 429 is consumed by this retry path.
                    # Release its HTTP socket before the backoff delay rather
                    # than waiting for GC while the local hub is saturated.
                    exc.close()
                if (not isinstance(details,dict) or
                        details.get("error")!="MODULE_INBOX_FULL_RETRY_LATER" or
                        details.get("accepted") is not False or
                        details.get("retryable") is not True or attempt==6):
                    raise exc
                time.sleep(min(0.5,0.025*(2**attempt)))

    def receive(self):
        """Non-destructive read with a bounded local mailbox probe.

        process_pending() holds the cross-process consumer lease while this
        runs. Never pin a standalone module for the generic 40-second RPC
        timeout merely because the hub is unavailable.
        """
        return self._request("/v1/inbox/peek",timeout=8)

    def ack(self, ids):
        """Confirm delivery with a bounded retry only for transport failure.

        ACK is idempotent at the hub: a lost reply is recoverable as
        already_acked. Unlike arbitrary tool RPCs, replaying this exact ACK
        cannot repeat the handler's operation or acknowledge a different ID.
        """
        payload={"ids":ids}
        # One 8-second budget across *all* idempotent attempts, not three
        # independent 8-second waits that can pin the consumer for 24 seconds.
        deadline=time.monotonic()+8.0
        last_error=None
        for attempt in range(3):
            remaining=deadline-time.monotonic()
            if attempt and remaining<=0:
                raise last_error
            timeout=8.0 if attempt==0 else min(8.0,remaining)
            try:
                return self._request("/v1/inbox/ack",payload,timeout=timeout)
            except HTTPError as exc:
                # HTTP rejection is not an uncertain transport outcome.
                exc.close()
                raise
            except (URLError, ConnectionError, TimeoutError) as exc:
                last_error=exc
                if attempt==2 or time.monotonic()>=deadline:
                    raise
                pause=min(0.05*(attempt+1),max(0.0,deadline-time.monotonic()))
                if pause:
                    time.sleep(pause)

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
            if (not isinstance(page,dict) or page.get("ok") is not True or
                    page.get("requires_ack") is not True):
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
                # A legacy inbox page may omit its recipient header.
                # An explicitly foreign target must still be refused before
                # invoking a handler or acknowledging any queued message.
                if (("target" in message and
                        message["target"] != CANONICAL_MODULE_ID) or
                        ("recipient" in page and
                         message.get("target") != CANONICAL_MODULE_ID)):
                    raise ValueError("MCP_INBOX_TARGET_MISMATCH")
                # Treat the hub response as an untrusted transport
                # envelope until every selected message passes preflight.
                # Do not execute or ACK forged/invalid source and topic data.
                source=message.get("source")
                topic=message.get("topic")
                cid=message.get("correlation_id","")
                if (type(source) is not str or
                        source not in ("mcp","bmollm","esp","android","easychange") or
                        type(topic) is not str or
                        not 1<=len(topic)<=100 or
                        any(not (c.isascii() and (c.isalnum() or c in "-._"))
                            for c in topic) or
                        type(cid) is not str or len(cid)>128):
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
    # Hub bearers are token_urlsafe() ASCII base64url. Reject Unicode,
    # whitespace and header-control characters before any network request;
    # urllib would otherwise fail later with a transport/encoding exception.
    if (type(token) is not str or not 32<=len(token)<=200 or
            not token.isascii() or
            any(not (char.isalnum() or char in "-_") for char in token)):
        raise ValueError("INVALID_HUB_CREDENTIAL")
    with _runtime_state_lock:
        members=MCPBridge(token).peers()
        if members.get("self")!=expected_module or members.get("ok") is not True:
            raise PermissionError("HUB_MODULE_IDENTITY_MISMATCH")
        _runtime_token=token
        _runtime_detached=False
        return {"connected":True,"id":expected_module}

def detach_hub(token):
    global _runtime_token, _runtime_detached
    # A host-spawned module may authenticate through its bootstrap env token
    # without a later attach_hub(). That exact credential must still be able
    # to revoke the local bridge; an explicit detach permanently disables
    # inherited-token fallback until authenticated attach_hub() succeeds.
    with _runtime_state_lock:
        expected=(_runtime_token or
                  ("" if _runtime_detached else
                   os.environ.get("MCP_MODULE_BUS_TOKEN","")))
        # compare_digest(str, str) rejects non-ASCII characters with
        # TypeError. Malformed local HTTP bodies must fail closed without an
        # uncaught exception or mutation of the active bridge credential.
        if (type(token) is not str or not 1<=len(token)<=256 or
                not token.isascii() or type(expected) is not str or
                not 1<=len(expected)<=256 or not expected.isascii() or
                not hmac.compare_digest(token,expected)):
            raise PermissionError("INVALID_DETACH_CREDENTIAL")
        _runtime_token=""
        _runtime_detached=True
        return {"connected":False}

def self_test():
    return {"standalone":not MCPBridge().connected,
            "hub_optional":True,"tools_from_mcp":True}
