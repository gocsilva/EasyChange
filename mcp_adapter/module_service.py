"""Read-only EasyChange module bridge, independent of MCP/ESP for standalone GUI."""
from __future__ import annotations
import argparse
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import urlopen
from mcp_adapter.integration import module_status,capabilities

def esp_online():
    try:
        with urlopen("http://127.0.0.1:8782/health",timeout=0.6) as response:
            return json.load(response).get("ok") is True
    except Exception:
        return False

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
            request=json.loads(self.rfile.read(size))
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
                return self.send(200,{"connected":True,"identity":members.get("self"),
                                      "peers":members.get("peers",[]),
                                      "tools":len(bridge.catalog().get("tools",[]))})
            except Exception as exc:
                return self.send(503,{"connected":False,"error":type(exc).__name__})
        if self.path=="/health":
            return self.send(200,{"ok":True,"id":"easychange","standalone":True})
        if self.path=="/status":
            return self.send(200,module_status(esp_enabled=esp_online()))
        if self.path=="/peers":
            return self.send(200,{"esp":{"connected":esp_online(),"required_for_mcp":True},
                                  "mcp":{"required_for_standalone":False}})
        if self.path=="/capabilities":
            return self.send(200,capabilities())
        return self.send(404,{"error":"NOT_FOUND"})
    def send(self,status,body):
        raw=json.dumps(body,ensure_ascii=False).encode("utf-8")
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
    if args.self_test:
        print(json.dumps(module_status()));return 0
    with ThreadingHTTPServer(("127.0.0.1",args.port),Handler) as server:
        server.serve_forever()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
