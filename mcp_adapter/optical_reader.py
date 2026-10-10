"""Read EC2 optical receipts from HDMI frames; do not infer execution success from HID."""
from __future__ import annotations
import binascii
import threading
import time
import zlib
from dataclasses import dataclass, field

# OpenCV's detector is reusable across frames, but should not be shared
# concurrently by unrelated HDMI readers.
_qr_detectors = threading.local()

from easychange.remote.optical_protocol import (
    decode_result_chunks, decode_ec2_packet_json, MAX_EC2_PACKETS,
    MAX_EC2_PACKET_TEXT_CHARS, MAX_EC2_TOKEN_CHARS,
)

MAX_EC2_ASSEMBLY_TEXT_CHARS = 48 * 1024 * 1024
MAX_EC2_ASSEMBLY_IDLE_SECONDS = 120.0

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
    pending_provisional_header: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.monotonic)

    def _reset_unverified(self):
        self.sequence=""
        self.chunks.clear()
        self.total=0
        self.stored_chars=0
        self.stored_token_chars=0
        self.packet_identity=None
        self.verified_payload=None
        self.header={}
        self.pending_provisional_header={}
        self.updated_at=time.monotonic()

    def ingest(self, payloads: list[str]) -> dict:
        # A partial or completed carousel must not own a reusable receiver
        # indefinitely. Expire only when new frames arrive: ingest([]) is
        # allowed to retrieve the last verified receipt without mutation.
        if (payloads and self.sequence
                and time.monotonic() - self.updated_at >= MAX_EC2_ASSEMBLY_IDLE_SECONDS):
            self._reset_unverified()
        for raw in payloads:
            try:
                if (not isinstance(raw,str) or
                        len(raw)>MAX_EC2_PACKET_TEXT_CHARS):
                    continue
                item=decode_ec2_packet_json(raw)
                if not isinstance(item,dict):continue
                tag=str(item.get("p") or "")
                if tag == "EC2H":
                    # EC2H d=1, n=0, w=0 is a durable *provisional* receipt.
                    # It hashes the original durable result, not the compact
                    # EC2 wire projection. Treat it as informational only.
                    # Final EC2H receipts must match the verified wire digest.
                    digest=item.get("h")
                    provisional=(type(item.get("d")) is int and item.get("d")==1
                                 and type(item.get("n")) is int and item.get("n")==0
                                 and type(item.get("w")) is int and item.get("w")==0)
                    valid_header=(item.get("f") is True
                                  and isinstance(digest,str)
                                  and len(digest) in (16,64)
                                  and all(ch in "0123456789abcdef" for ch in digest))
                    if self.packet_identity is None:
                        # A durable EC2H page may precede asynchronous QR
                        # compression. Preserve one bounded untrusted header,
                        # but do not claim a verified result or a sequence yet.
                        if provisional and valid_header and item.get("s"):
                            self.pending_provisional_header=item
                        continue
                    if (str(item.get("s") or "") == self.sequence
                            and valid_header
                            and (provisional or digest[:16] == self.packet_identity[2])):
                        # Never replace a matching final header with an
                        # unverified provisional receipt arriving later.
                        existing_final=(self.header and
                                        not (type(self.header.get("d")) is int
                                             and self.header.get("d")==1))
                        if not (provisional and existing_final):
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
                    if decode_ec2_packet_json(prev)!=item:continue
                candidate=self.stored_chars - (len(prev) if prev is not None else 0) + len(raw)
                token_candidate=self.stored_token_chars + (len(piece) if prev is None else 0)
                if (candidate>MAX_EC2_ASSEMBLY_TEXT_CHARS or
                        token_candidate>MAX_EC2_TOKEN_CHARS):
                    continue
                if not self.sequence:
                    self.sequence=seq;self.total=count
                    self.packet_identity=identity
                    # Bind an earlier provisional receipt only to the same
                    # sequence; it never bypasses EC2 CRC/SHA verification.
                    if self.pending_provisional_header.get("s")==seq:
                        self.header=self.pending_provisional_header
                    self.pending_provisional_header={}
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
                try:
                    self.verified_payload=decode_result_chunks(self.chunks.values())
                except (ValueError, TypeError, KeyError, RecursionError, zlib.error):
                    # Every index was occupied, yet the assembled result was
                    # not authentic. Do not let a CRC-consistent but corrupted
                    # carousel block a later valid retransmission forever.
                    # Preserve the integrity failure for callers and discard
                    # only this unverified session.
                    self._reset_unverified()
                    raise
            if self.header:
                digest=str(self.header.get("h") or "")
                # EC2H d=1 advertises the original durable receipt digest,
                # whereas EC2 fragments hash the compact optical projection.
                # Fragment SHA/CRC is still independently verified by decode.
                # A final EC2H (without d=1) must match the wire digest.
                provisional=(type(self.header.get("d")) is int
                             and self.header.get("d")==1
                             and self.header.get("n")==0)
                if (digest and not provisional and
                        digest[:16] != str(self.packet_identity[2] or "")):
                    raise ValueError("EC2_HEADER_HASH_MISMATCH")
            return {"complete":True,"verified":True,"sequence":self.sequence,
                    "payload":self.verified_payload,"packets":self.total}
        return {"complete":False,"sequence":self.sequence or None,"received":len(self.chunks),
                "expected":self.total or None,"header":self.header}

def decode_hdmi_qr(frame) -> list[str]:
    """Use image pixels as the sole observational source; never access REMOTE_PC filesystem."""
    import cv2
    detector=getattr(_qr_detectors,"detector",None)
    if detector is None:
        detector=cv2.QRCodeDetector()
        _qr_detectors.detector=detector
    ok, texts, _points, _rectified=detector.detectAndDecodeMulti(frame)
    decoded=[value for value in texts if value] if ok and texts else []
    if not decoded:
        single,_points,_rectified=detector.detectAndDecode(frame)
        if single:decoded=[single]
    return decoded
