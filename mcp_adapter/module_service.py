"""Read-only EasyChange module bridge, independent of MCP/ESP for standalone GUI."""
from __future__ import annotations
import argparse
import json
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
        status=json.loads(body,object_pairs_hook=_unique_service_fields)
        # The loopback port is not an identity: another service may occupy it.
        return (isinstance(status,dict) and status.get("ok") is True
                and status.get("id")=="esp")
    except Exception:
        return False

def _unique_service_fields(pairs):
    fields={}
    for name,value in pairs:
        if name in fields:
            raise ValueError("MODULE_SERVICE_DUPLICATE_JSON_KEY")
        fields[name]=value
    return fields

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def do_POST(self):
        if self.path not in ("/v1/attach","/v1/detach"):
            return self.send(404,{"error":"NOT_FOUND"})
        if self.headers.get("Origin"):
            return self.send(403,{"error":"BROWSER_ORIGIN_NOT_ALLOWED"})
        if self.headers.get("Content-Type","").split(";")[0].strip().lower()!="application/json":
            return self.send(415,{"error":"JSON_REQUIRED"})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=512:
                return self.send(413,{"error":"PAYLOAD_TOO_LARGE"})
            request=json.loads(self.rfile.read(size),object_pairs_hook=_unique_service_fields)
            if not isinstance(request,dict):
                raise ValueError("ATTACH_BODY_REQUIRED")
            from module_bridge import attach_hub,detach_hub
            token=request.get("token")
            if self.path=="/v1/attach":
                return self.send(200,attach_hub(token,"easychange"))
            return self.send(200,detach_hub(token))
        except (ValueError,TypeError,PermissionError) as exc:
            return self.send(403,{"error":type(exc).__name__})

    def do_GET(self):
        if self.path=="/hub":
            from module_bridge import MCPBridge
            bridge=MCPBridge()
            if not bridge.connected:
                return self.send(200,{"connected":False,"standalone":True})
            try:
                members=bridge.peers()
                if (not isinstance(members,dict) or members.get("ok") is not True
                        or members.get("self")!="easychange"):
                    return self.send(503,{"connected":False,
                                          "error":"MCP_MODULE_IDENTITY_MISMATCH"})
                return self.send(200,{"connected":True,"identity":members.get("self"),
                                      "peers":members.get("peers",[]),
                                      "tools":len(bridge.catalog().get("tools",[]))})
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
            raw=json.dumps(body,ensure_ascii=False,allow_nan=False).encode("utf-8")
        except (TypeError,ValueError,OverflowError,RecursionError):
            # Match the other standalone module servers: a malformed status
            # must never emit non-standard JSON or crash an HTTP request.
            status=502
            raw=b'{"error":"INVALID_SERVICE_RESPONSE"}'
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
    with ThreadingHTTPServer(("127.0.0.1",args.port),Handler) as server:
        server.serve_forever()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
