"""Read EC2 optical receipts from HDMI frames; do not infer execution success from HID."""
from __future__ import annotations
import binascii
import itertools
import threading
import time
import zlib
from dataclasses import dataclass, field

# OpenCV's detector is reusable across frames, but should not be shared
# concurrently by unrelated HDMI readers.
_qr_detectors = threading.local()

from easychange.remote.optical_protocol import (
    decode_result_chunks, decode_ec2_packet_json, valid_ec2_identity, MAX_EC2_PACKETS,
    MAX_EC2_PACKET_TEXT_CHARS, MAX_EC2_TOKEN_CHARS,
)

MAX_EC2_ASSEMBLY_TEXT_CHARS = 48 * 1024 * 1024
MAX_EC2_ASSEMBLY_IDLE_SECONDS = 120.0
# A single HDMI poll yields at most 32 decoded symbols. Keep separately
# bounded bulk ingest() calls from spending unbounded time on ignored frames.
# Larger transfers remain supported across successive bounded poll batches.
MAX_EC2_INGEST_FRAME_LIMIT = 4096
MAX_EC2_INGEST_TEXT_CHARS = 8 * 1024 * 1024

@dataclass
class OpticalAssembler:
    sequence: str = ""
    chunks: dict[int, str] = field(default_factory=dict)
    total: int = 0
    stored_chars: int = 0
    stored_token_chars: int = 0
    # Exact, previously validated QR repeated in successive HDMI frames.
    # Keep only one string reference rather than a second packet cache.
    last_admitted_frame: str | None = None
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
        self.last_admitted_frame=None
        self.packet_identity=None
        self.verified_payload=None
        self.header={}
        self.pending_provisional_header={}
        self.updated_at=time.monotonic()

    def ingest(self, payloads: list[str]) -> dict:
        # Preflight before touching the active session. Even ignored invalid
        # frames cost JSON parsing and can starve a real HDMI QR transfer.
        if len(payloads) > MAX_EC2_INGEST_FRAME_LIMIT:
            raise ValueError("EC2_INGEST_FRAME_LIMIT")
        text_chars = 0
        for candidate in payloads:
            if isinstance(candidate, str):
                text_chars += len(candidate)
                if text_chars > MAX_EC2_INGEST_TEXT_CHARS:
                    raise ValueError("EC2_INGEST_TEXT_LIMIT")
        # A partial or completed carousel must not own a reusable receiver
        # indefinitely. Expire only when new frames arrive: ingest([]) is
        # allowed to retrieve the last verified receipt without mutation.
        if (payloads and (self.sequence or self.pending_provisional_header)
                and time.monotonic() - self.updated_at >= MAX_EC2_ASSEMBLY_IDLE_SECONDS):
            self._reset_unverified()
        for raw in payloads:
            try:
                if (not isinstance(raw,str) or
                        len(raw)>MAX_EC2_PACKET_TEXT_CHARS):
                    continue
                # HDMI frequently repeats the identical decoded symbol for
                # multiple video frames. It has already passed the complete
                # identity/base64/CRC gate and still occupies our exact
                # current transfer; skip JSON parsing on subsequent frames.
                if self.sequence and raw==self.last_admitted_frame:
                    self.updated_at=time.monotonic()
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
                            self.updated_at=time.monotonic()
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
                # Do not let corrupt transfer metadata occupy the first slot:
                # a later authentic retransmission must remain admissible.
                if not valid_ec2_identity(item):
                    continue
                seq=item["s"]
                index=item.get("i"); count=item.get("n")
                if (not seq or type(count) is not int or
                        count<1 or count>MAX_EC2_PACKETS or
                        type(index) is not int or index<0 or index>=count):
                    continue
                # Do not claim a slot from changed metadata until the
                # incoming fragment's own CRC has been verified below.
                # Literal-metadata marker must agree across the same EC2
                # carousel. Reject malformed markers before claiming a slot,
                # otherwise a later valid retransmission could be blocked.
                marker=item.get("t")
                if "t" in item and (type(marker) is not int or marker!=0):
                    continue
                identity=(seq,count,item.get("h"),item.get("c"),item.get("e"),marker)
                piece=item.get("d")
                if not isinstance(piece,str) or len(piece)>MAX_EC2_TOKEN_CHARS:
                    continue
                # HDMI burst repeats the same accepted QR many times. An
                # identical byte-for-byte frame in this exact transfer was
                # already CRC-checked and is safe to retain without another
                # base64 scan, UTF-8 copy or CRC calculation.
                if (self.packet_identity is not None
                        and identity==self.packet_identity
                        and self.chunks.get(index)==raw):
                    self.last_admitted_frame=raw
                    self.updated_at=time.monotonic()
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
                if self.sequence and seq==self.sequence and identity!=self.packet_identity:
                    # Raw single-line ESP commands legitimately reuse the
                    # unsequenced EC2 identifier "-". After a COMPLETE,
                    # verified carousel, a new CRC-valid index 0 with a
                    # different digest starts a new result; never merge it
                    # with an old read/search/write receipt. Sequenced MCP
                    # operations remain immutable even after completion.
                    if (seq=="-" and index==0
                            and self.verified_payload is not None):
                        self._reset_unverified()
                    else:
                        continue
                if self.sequence and seq!=self.sequence:
                    # A completed receipt may hand off to the next valid
                    # carousel immediately. Incomplete work stays protected
                    # against mixing until its idle timeout.
                    if self.verified_payload is None or index != 0:
                        continue
                    self._reset_unverified()
                if self.packet_identity is not None and identity!=self.packet_identity:
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
                self.last_admitted_frame=raw
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
    """Decode observed pixels only; a stale/invalid capture is not a QR result."""
    # HDMI capture resets can temporarily deliver None or a zero-sized frame.
    # Do not instantiate OpenCV's detector or crash the EC2 polling loop.
    shape=getattr(frame,"shape",None)
    if (not isinstance(shape,tuple) or len(shape) not in (2,3)
            or type(shape[0]) is not int or type(shape[1]) is not int
            or shape[0]<1 or shape[1]<1
            or shape[0]*shape[1]>67_108_864
            or (len(shape)==3 and
                (type(shape[2]) is not int or shape[2] not in (1,3,4)))):
        # A corrupt capture may report absurd dimensions. QR detection can
        # allocate multiple temporary images, so reject before calling cv2.
        return []
    import cv2
    # Real 1280x720 MJPG HDMI from EasyChange: OpenCV's multi-reader
    # repeatedly reported ZERO symbols for perfectly valid 4-cell EC2 QR,
    # while ZXing-C++ recovered all four from the identical physical frame.
    # Prefer ZXing; retain OpenCV as an independent fallback for rare cells.
    zxing_values=[]
    try:
        import zxingcpp
        detected=zxingcpp.read_barcodes(
            frame, formats=zxingcpp.BarcodeFormat.QRCode,
            try_rotate=True, try_downscale=True,
        )
        seen_zx=set()
        for barcode in itertools.islice(detected,32):
            raw=getattr(barcode,"text",None)
            if isinstance(raw,str) and raw and raw not in seen_zx:
                seen_zx.add(raw)
                zxing_values.append(raw)
        if len(zxing_values)>=4:
            return zxing_values
    except (ImportError, AttributeError, TypeError, RuntimeError, ValueError):
        pass

    try:
        detector=getattr(_qr_detectors,"detector",None)
        if detector is None:
            # Decoder construction can fail transiently as a capture backend
            # is reset. Leave the thread-local slot empty for a later retry.
            detector=cv2.QRCodeDetector()
            _qr_detectors.detector=detector
        # Some OpenCV builds expose only the single-QR API. Do not make a
        # valid EC2 receipt unreadable merely because multi-QR is unavailable.
        multi=getattr(detector,"detectAndDecodeMulti",None)
        if callable(multi):
            decoded=[]
            try:
                ok, texts, _points, _rectified=multi(frame)
                if ok and texts is not None and not isinstance(texts,(str,bytes,dict)):
                    # The multi API may return generators rather than tuples.
                    # Never exhaust an unbounded iterator in the HDMI reader.
                    # Some decoders return a QR more than once. Count unique
                    # symbols so duplicates cannot hide a missing fourth footer
                    # QR or suppress the existing per-cell recovery pass.
                    seen=set()
                    for value in itertools.islice(texts,32):
                        if isinstance(value,str) and value and value not in seen:
                            seen.add(value)
                            decoded.append(value)
            except (TypeError, ValueError, cv2.error):
                # OpenCV builds can expose multi-QR yet fail at runtime due
                # to unsupported signatures or an internal decoder error.
                # Unexpected tuple sizes raise ValueError while unpacking.
                # Fall back to the single-QR API on this same observed frame.
                decoded=[]
        else:
            decoded=[]
        if not decoded and not zxing_values:
            single,_points,_rectified=detector.detectAndDecode(frame)
            if isinstance(single,str) and single:decoded=[single]
        # Valid ZXing results must survive OpenCV returning zero or throwing.
        decoded=list(dict.fromkeys([*zxing_values,*decoded]))

        # OpenCV multi-detection may see only 3/4 QR cells on a compact
        # EasyChange footer at 1080p. Decode four overlapping footer crops
        # individually without changing the EC2 integrity-verification path.
        height, width = shape[:2]
        if 0 < len(decoded) < 4 and width >= 1000 and height >= 600 and width >= height:
            left = int(width * 0.305)
            top = int(height * 0.63)
            column = (width - left) / 4
            overlap = max(8, int(column * 0.045))
            seen = set(decoded)
            for slot in range(4):
                x0 = max(0, int(left + slot * column) - overlap)
                x1 = min(width, int(left + (slot + 1) * column) + overlap)
                if x1 <= x0:
                    continue
                try:
                    crop = frame[top:height, x0:x1]
                    value, _points, _rectified = detector.detectAndDecode(crop)
                except (TypeError, AttributeError, IndexError, cv2.error):
                    # A minimal/non-sliceable frame must preserve the QR
                    # symbols already decoded from its full-frame content.
                    continue
                if isinstance(value, str) and value and value not in seen:
                    seen.add(value)
                    decoded.append(value)
                    if len(decoded) == 4:
                        break
        return decoded
    except (cv2.error, TypeError, ValueError):
        # The ZXing result is independently decodable on this SAME image.
        # Never erase valid QR strings merely because OpenCV threw.
        return zxing_values
