"""Read EC2 optical receipts from HDMI frames; do not infer execution success from HID."""
from __future__ import annotations
import binascii
import json
import time
from dataclasses import dataclass, field

from easychange.remote.optical_protocol import (
    decode_result_chunks, MAX_EC2_PACKETS,
    MAX_EC2_PACKET_TEXT_CHARS, MAX_EC2_TOKEN_CHARS,
)

MAX_EC2_ASSEMBLY_TEXT_CHARS = 48 * 1024 * 1024

@dataclass
class OpticalAssembler:
    sequence: str = ""
    chunks: dict[int, str] = field(default_factory=dict)
    total: int = 0
    stored_chars: int = 0
    stored_token_chars: int = 0
    packet_identity: tuple | None = None
    verified_payload: dict | None = None
    header: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.monotonic)

    def ingest(self, payloads: list[str]) -> dict:
        for raw in payloads:
            try:
                if (not isinstance(raw,str) or
                        len(raw)>MAX_EC2_PACKET_TEXT_CHARS):
                    continue
                item=json.loads(raw)
                if not isinstance(item,dict):continue
                tag=str(item.get("p") or "")
                if tag == "EC2H":
                    # A QR claiming completion is still untrusted until its
                    # digest matches the actual EC2 stream being assembled.
                    digest=item.get("h")
                    if (self.packet_identity is not None
                            and str(item.get("s") or "") == self.sequence
                            and item.get("f") is True
                            and isinstance(digest,str)
                            and len(digest) in (16,64)
                            and all(ch in "0123456789abcdef" for ch in digest)
                            and digest[:16] == self.packet_identity[2]):
                        self.header=item
                    continue
                if tag!="EC2":continue
                seq=str(item.get("s") or "")
                index=item.get("i"); count=item.get("n")
                if (not seq or type(count) is not int or
                        count<1 or count>MAX_EC2_PACKETS or
                        type(index) is not int or index<0 or index>=count):
                    continue
                if self.sequence and seq!=self.sequence:continue
                if self.sequence and count!=self.total:
                    continue
                identity=(seq,count,item.get("h"),item.get("c"),item.get("e"))
                if self.packet_identity is not None and identity!=self.packet_identity:
                    continue
                piece=item.get("d")
                if not isinstance(piece,str) or len(piece)>MAX_EC2_TOKEN_CHARS:
                    continue
                # Reject corrupted HDMI QR fragments before reserving their
                # index. A later valid retransmission must remain admissible.
                try:
                    piece_bytes=piece.encode("ascii")
                except UnicodeEncodeError:
                    continue
                if any(ch not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in piece_bytes):
                    continue
                expected_crc=item.get("x")
                if (not isinstance(expected_crc,str) or
                        len(expected_crc)!=8 or
                        f"{binascii.crc32(piece_bytes) & 0xffffffff:08x}"!=expected_crc):
                    continue
                prev=self.chunks.get(index)
                if prev is not None and prev!=raw:
                    # Equivalent duplicate packet may differ only by whitespace.
                    if json.loads(prev)!=item:continue
                candidate=self.stored_chars - (len(prev) if prev is not None else 0) + len(raw)
                token_candidate=self.stored_token_chars + (len(piece) if prev is None else 0)
                if (candidate>MAX_EC2_ASSEMBLY_TEXT_CHARS or
                        token_candidate>MAX_EC2_TOKEN_CHARS):
                    continue
                if not self.sequence:
                    self.sequence=seq;self.total=count
                    self.packet_identity=identity
                self.chunks[index]=raw
                self.stored_chars=candidate
                self.stored_token_chars=token_candidate
                self.updated_at=time.monotonic()
            except (ValueError,TypeError,KeyError,RecursionError):
                continue
        if self.total and len(self.chunks)==self.total:
            # In HDMI burst mode the same complete carousel is observed many
            # times. Decode and validate the identical verified payload once.
            if self.verified_payload is None:
                self.verified_payload=decode_result_chunks(list(self.chunks.values()))
            if self.header:
                digest=str(self.header.get("h") or "")
                if digest and digest[:16] != str(self.packet_identity[2] or ""):
                    raise ValueError("EC2_HEADER_HASH_MISMATCH")
            return {"complete":True,"verified":True,"sequence":self.sequence,
                    "payload":self.verified_payload,"packets":self.total}
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
