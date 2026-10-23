import pytest
from easychange.remote import gatt_wire
from easychange.remote.gatt_wire import FramedMessages, frame_object, fragments

def test_fragmented_bidirectional_messages():
    items=[{"type":"hello","pk":"a"*80},{"type":"result","data":"x"*8000}]
    reader=FramedMessages()
    output=[]
    for item in items:
        for part in fragments(frame_object(item)):
            output.extend(reader.feed(part))
    assert output==items

def test_invalid_frame_rejected_and_buffer_cleared():
    reader=FramedMessages()
    with pytest.raises(ValueError):
        reader.feed(b"\xff\xff\xff\xff")
    assert list(fragments(frame_object({"ok":True})))
    assert reader.feed(frame_object({"ok":True}))==[{"ok":True}]

def test_coalesced_legal_frames_near_buffer_limit(monkeypatch):
    monkeypatch.setattr(gatt_wire, "MAX_MESSAGE", 64)
    monkeypatch.setattr(gatt_wire, "MAX_BUFFER", 68)
    large, next_item = {"d": "x" * 45}, {"d": "ok"}
    joined = frame_object(large) + frame_object(next_item)
    assert len(joined) > gatt_wire.MAX_BUFFER
    assert len(joined) <= gatt_wire.CHUNK_BYTES
    assert FramedMessages().feed(joined) == [large, next_item]


def test_bad_json_discards_following_buffered_commands():
    reader = FramedMessages()
    malformed = b"{oops"
    later = frame_object({"command": "later"})
    packet = len(malformed).to_bytes(4, "big") + malformed + later
    with pytest.raises(ValueError, match="GATT_INVALID_FRAME_JSON"):
        reader.feed(packet)
    # The old bug retained 'later', which was then delivered twice.
    assert reader.feed(later) == [{"command": "later"}]


def test_invalid_utf8_frame_clears_buffer():
    reader = FramedMessages()
    malformed = b"\xff"
    with pytest.raises(ValueError, match="GATT_INVALID_FRAME_JSON"):
        reader.feed(b"\x00\x00\x00\x01" + malformed)
    assert reader.feed(frame_object({"healthy": True})) == [{"healthy": True}]
