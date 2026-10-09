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
    def do_GET(self):
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
