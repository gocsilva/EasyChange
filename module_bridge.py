"""Optional MCP hub client; independent process can join the authenticated MCP host."""
from __future__ import annotations
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen

BUS=os.environ.get("MCP_MODULE_BUS_URL","http://127.0.0.1:8786")
if BUS not in ("http://127.0.0.1:8786","http://localhost:8786"):
    raise ValueError("MCP_BUS_MUST_USE_LOOPBACK")

_runtime_token=""

class MCPBridge:
    def __init__(self,token=None):
        self.token=token if token is not None else (_runtime_token or os.environ.get("MCP_MODULE_BUS_TOKEN",""))
        self.module=os.environ.get("MCP_MODULE_ID","")

    @property
    def connected(self):
        return bool(self.token)

    def _request(self,path,body=None,*,timeout=40):
        if not self.token:raise RuntimeError("MCP_NOT_CONNECTED_STANDALONE")
        if path not in ("/v1/tools","/v1/tools/call","/v1/peers","/v1/send","/v1/inbox"):
            raise ValueError("MCP_BUS_PATH_NOT_ALLOWED")
        headers={"Authorization":"Bearer "+self.token}
        payload=None
        if body is not None:
            payload=json.dumps(body,ensure_ascii=False).encode("utf-8")
            headers["Content-Type"]="application/json"
        req=Request(BUS+path,data=payload,method="POST" if payload is not None else "GET",headers=headers)
        with urlopen(req,timeout=timeout) as response:
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
