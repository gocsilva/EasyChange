"""Read-only EasyChange module bridge, independent of MCP/ESP for standalone GUI."""
from __future__ import annotations
import argparse
import json
import math
import threading
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import ProxyHandler, HTTPRedirectHandler, build_opener
from mcp_adapter.integration import module_status,capabilities

class _NoLocalPeerRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

# ESP presence must not reach any remote host through a proxy or redirect.
urlopen = build_opener(ProxyHandler({}), _NoLocalPeerRedirect()).open

def esp_online():
    try:
        with urlopen("http://127.0.0.1:8782/health",timeout=0.6) as response:
            body=response.read(2049)
        if len(body)>2048:
            return False
        status=json.loads(body,object_pairs_hook=_unique_service_fields,
                          parse_float=_finite_service_json_number,
                          parse_constant=_finite_service_json_number)
        # The loopback port is not an identity: another service may occupy it.
        return (isinstance(status,dict) and status.get("ok") is True
                and status.get("id")=="esp")
    except HTTPError as exc:
        # Non-2xx responses are raised before the with-response cleanup
        # begins. Close the rejected peer reply before reporting offline.
        exc.close()
        return False
    except Exception:
        return False

def _finite_service_json_number(token):
    value=float(token)
    if not math.isfinite(value):
        raise ValueError("MODULE_SERVICE_NONFINITE_JSON")
    return value


def _unique_service_fields(pairs):
    fields={}
    for name,value in pairs:
        if name in fields:
            raise ValueError("MODULE_SERVICE_DUPLICATE_JSON_KEY")
        fields[name]=value
    return fields

MAX_SERVICE_RESPONSE_BYTES = 2 * 1024 * 1024

def _bounded_service_json(value):
    """Stream-encode standalone HTTP responses with a strict UTF-8 byte cap."""
    chunks=bytearray()
    for piece in json.JSONEncoder(ensure_ascii=False,allow_nan=False).iterencode(value):
        if len(piece)>MAX_SERVICE_RESPONSE_BYTES:
            raise ValueError("MODULE_SERVICE_RESPONSE_TOO_LARGE")
        # JSONEncoder may yield one enormous Unicode fragment. Encode in
        # small windows so the byte limit also bounds temporary allocations.
        for offset in range(0,len(piece),4096):
            raw=piece[offset:offset+4096].encode("utf-8")
            if len(chunks)+len(raw)>MAX_SERVICE_RESPONSE_BYTES:
                raise ValueError("MODULE_SERVICE_RESPONSE_TOO_LARGE")
            chunks.extend(raw)
    return bytes(chunks)

class BoundedLocalHTTPServer(ThreadingHTTPServer):
    """Cap live HTTP workers independently of the socket accept backlog."""
    request_queue_size = 64
    max_client_threads = 64
    daemon_threads = True
    block_on_close = False

    def __init__(self,*args,**kwargs):
        self._client_slots=threading.BoundedSemaphore(self.max_client_threads)
        super().__init__(*args,**kwargs)

    def process_request(self,request,client_address):
        if not self._client_slots.acquire(blocking=False):
            # Never allocate a worker for an overloaded local status probe.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request,client_address)
        except BaseException:
            self._client_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self,request,client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self._client_slots.release()

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        # Bound idle socket I/O; this does not cap elapsed module work.
        self.request.settimeout(12.0)
        super().setup()

    def log_message(self,*args):
        pass
    def do_POST(self):
        if self.path not in ("/v1/attach","/v1/detach"):
            return self.send(404,{"error":"NOT_FOUND"})
        if "Origin" in self.headers:
            return self.send(403,{"error":"BROWSER_ORIGIN_NOT_ALLOWED"})
        if self.headers.get("Content-Type","").split(";")[0].strip().lower()!="application/json":
            return self.send(415,{"error":"JSON_REQUIRED"})
        try:
            raw_lengths=(self.headers.get_all("Content-Length")
                         if hasattr(self.headers,"get_all")
                         else [self.headers.get("Content-Length","0")])
            if raw_lengths is not None and len(raw_lengths)!=1:
                return self.send(400,{"error":"INVALID_CONTENT_LENGTH"})
            length=(raw_lengths[0] if raw_lengths else "0")
            # Control bodies are capped at 512 bytes. Validate the header
            # before converting attacker-controlled digits into a Python int.
            if (type(length) is not str or not 1<=len(length)<=10 or
                    not length.isascii() or not length.isdecimal()):
                return self.send(400,{"error":"INVALID_CONTENT_LENGTH"})
            size=int(length)
            if not 0<size<=512:
                return self.send(413,{"error":"PAYLOAD_TOO_LARGE"})
            request=json.loads(
                self.rfile.read(size), object_pairs_hook=_unique_service_fields,
                parse_float=_finite_service_json_number,
                parse_constant=_finite_service_json_number)
            if not isinstance(request,dict):
                raise ValueError("ATTACH_BODY_REQUIRED")
            from module_bridge import attach_hub,detach_hub
            token=request.get("token")
            if self.path=="/v1/attach":
                return self.send(200,attach_hub(token,"easychange"))
            return self.send(200,detach_hub(token))
        except (ValueError,TypeError,PermissionError) as exc:
            return self.send(403,{"error":type(exc).__name__})
        except HTTPError as exc:
            # HTTPError owns an underlying HTTP response/socket. Repeated
            # attach retries must not leak rejected response descriptors.
            try:
                if exc.code in (401,403):
                    return self.send(403,{"error":"HUB_CREDENTIAL_REJECTED"})
                return self.send(503,{"error":"HUB_UNAVAILABLE"})
            finally:
                exc.close()
        except (ImportError,OSError,RuntimeError):
            # Do not strand attach callers when MCP is offline or times out.
            # Keep the credential/error internals out of the HTTP response.
            return self.send(503,{"error":"HUB_UNAVAILABLE"})

    def do_GET(self):
        if self.path=="/hub":
            try:
                from module_bridge import MCPBridge
                bridge=MCPBridge()
            except (ImportError,RuntimeError,OSError):
                # The optional MCP client must never crash a standalone
                # module's HTTP status endpoint during import or startup.
                return self.send(503,{"connected":False,
                                    "error":"HUB_CLIENT_UNAVAILABLE"})
            if not bridge.connected:
                return self.send(200,{"connected":False,"standalone":True})
            try:
                members=bridge.peers()
                if (not isinstance(members,dict) or members.get("ok") is not True
                        or members.get("self")!="easychange"):
                    return self.send(503,{"connected":False,
                                          "error":"MCP_MODULE_IDENTITY_MISMATCH"})
                catalog=bridge.catalog()
                peers=members.get("peers")
                tools=catalog.get("tools") if isinstance(catalog,dict) else None
                if (not isinstance(catalog,dict) or catalog.get("ok") is not True
                        or not isinstance(tools,list) or len(tools)>4096
                        or not isinstance(peers,list)
                        or any(not isinstance(peer,str) for peer in peers)):
                    return self.send(503,{"connected":False,
                                          "error":"MCP_MODULE_CATALOG_INVALID"})
                return self.send(200,{"connected":True,"identity":members.get("self"),
                                      "peers":peers,"tools":len(tools)})
            except HTTPError as exc:
                # Close the rejected MCP response even when the local send fails.
                try:
                    return self.send(503,{"connected":False,"error":"HTTPError"})
                finally:
                    exc.close()
            except Exception as exc:
                return self.send(503,{"connected":False,"error":type(exc).__name__})
        if self.path=="/health":
            return self.send(200,{"ok":True,"id":"easychange","standalone":True})
        if self.path=="/status":
            try:
                return self.send(200,module_status(esp_enabled=esp_online()))
            except Exception:
                return self.send(503,{"error":"MODULE_STATUS_UNAVAILABLE"})
        if self.path=="/peers":
            return self.send(200,{"esp":{"connected":esp_online(),"required_for_mcp":True},
                                  "mcp":{"required_for_standalone":False}})
        if self.path=="/capabilities":
            try:
                return self.send(200,capabilities())
            except Exception:
                return self.send(503,{"error":"MODULE_CAPABILITIES_UNAVAILABLE"})
        return self.send(404,{"error":"NOT_FOUND"})
    def send(self,status,body):
        try:
            raw=_bounded_service_json(body)
        except (TypeError,ValueError,OverflowError,RecursionError,UnicodeError) as exc:
            status=502
            raw=(b'{"error":"MODULE_SERVICE_RESPONSE_TOO_LARGE"}'
                 if isinstance(exc,ValueError) and
                    str(exc)=="MODULE_SERVICE_RESPONSE_TOO_LARGE"
                 else b'{"error":"INVALID_SERVICE_RESPONSE"}')
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8784)
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise ValueError("INVALID_PORT")
    if args.self_test:
        print(json.dumps(module_status()));return 0
    with BoundedLocalHTTPServer(("127.0.0.1",args.port),Handler) as server:
        server.serve_forever()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
