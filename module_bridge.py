"""Optional MCP hub client for a module process started from its .mdl.

This library does not import MCP, BMO, ESP, Android or EasyChange. Without a
token it simply reports that the independent module is disconnected.
"""
from __future__ import annotations
import json
import os
from urllib.request import Request, urlopen

BUS = os.environ.get("MCP_MODULE_BUS_URL","http://127.0.0.1:8786")
if BUS not in ("http://127.0.0.1:8786","http://localhost:8786"):
    raise ValueError("MCP_BUS_MUST_USE_LOOPBACK")

class MCPBridge:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("MCP_MODULE_BUS_TOKEN","")
        self.module = os.environ.get("MCP_MODULE_ID","")
    @property
    def connected(self):
        return bool(self.token)

    def _request(self, path, body=None, *, timeout=40):
        if not self.token:
            raise RuntimeError("MCP_NOT_CONNECTED_STANDALONE")
        if path not in ("/v1/tools","/v1/tools/call","/v1/peers","/v1/send","/v1/inbox"):
            raise ValueError("MCP_BUS_PATH_NOT_ALLOWED")
        headers={"Authorization":"Bearer "+self.token}
        raw=None
        if body is not None:
            raw=json.dumps(body,ensure_ascii=False).encode("utf-8")
            headers["Content-Type"]="application/json"
        req=Request(BUS+path,data=raw,method="POST" if raw is not None else "GET",headers=headers)
        with urlopen(req,timeout=timeout) as response:
            data=json.load(response)
        if not isinstance(data,dict):
            raise ValueError("INVALID_MCP_HUB_RESPONSE")
        return data

    def catalog(self):
        """All currently registered MCP tools, including connected add-ons."""
        return self._request("/v1/tools")

    def call(self,tool,**arguments):
        """Execute the actual MCP tool; its original permission checks still apply."""
        return self._request("/v1/tools/call",{"tool":tool,"arguments":arguments},timeout=120)

    def peers(self):
        return self._request("/v1/peers")

    def send(self,target,topic,payload=None,correlation_id=""):
        return self._request("/v1/send",{"target":target,"topic":topic,
                                        "payload":payload,"correlation_id":correlation_id})

    def receive(self):
        return self._request("/v1/inbox")

def self_test():
    return {"standalone":not bool(os.environ.get("MCP_MODULE_BUS_TOKEN")),
            "hub_optional":True, "tools_from_mcp":True}
