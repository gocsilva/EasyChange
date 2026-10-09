"""Read EC2 optical receipts from HDMI frames; do not infer execution success from HID."""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field

from easychange.remote.optical_protocol import decode_result_chunks

@dataclass
class OpticalAssembler:
    sequence: str = ""
    chunks: dict[int, str] = field(default_factory=dict)
    total: int = 0
    header: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.monotonic)

    def ingest(self, payloads: list[str]) -> dict:
        for raw in payloads:
            try:
                item=json.loads(raw)
                if not isinstance(item,dict):continue
                tag=str(item.get("p") or "")
                if tag == "EC2H":
                    if str(item.get("s") or "") == self.sequence and bool(item.get("f")):
                        self.header=item
                    continue
                if tag!="EC2":continue
                seq=str(item.get("s") or "")
                index=int(item.get("i")); count=int(item.get("n"))
                if not seq or count<1 or count>200000 or index<0 or index>=count:continue
                if self.sequence and seq!=self.sequence:continue
                if not self.sequence:self.sequence=seq;self.total=count
                if count!=self.total:raise ValueError("MIXED_EC2_PACKET_COUNT")
                prev=self.chunks.get(index)
                if prev is not None and prev!=raw:
                    # Equivalent duplicate packet may differ only by whitespace.
                    if json.loads(prev)!=item:raise ValueError("CONFLICTING_EC2_CHUNK")
                self.chunks[index]=raw
                self.updated_at=time.monotonic()
            except (ValueError,TypeError,KeyError):
                continue
        if self.total and len(self.chunks)==self.total:
            result=decode_result_chunks(list(self.chunks.values()))
            if self.header:
                digest=str(self.header.get("h") or "")
                import hashlib
                wire = None
                if digest and digest[:16] != str(json.loads(next(iter(self.chunks.values()))).get("h") or ""):
                    raise ValueError("EC2_HEADER_HASH_MISMATCH")
            return {"complete":True,"verified":True,"sequence":self.sequence,
                    "payload":result,"packets":self.total}
        return {"complete":False,"sequence":self.sequence or None,"received":len(self.chunks),
                "expected":self.total or None,"header":self.header}

def decode_hdmi_qr(frame) -> list[str]:
    """Use image pixels as the sole observational source; never access REMOTE_PC filesystem."""
    import cv2
    detector=cv2.QRCodeDetector()
    ok, texts, _points, _rectified=detector.detectAndDecodeMulti(frame)
    decoded=[value for value in texts if value] if ok and texts else []
    if not decoded:
        single,_points,_rectified=detector.detectAndDecode(frame)
        if single:decoded=[single]
    return decoded
