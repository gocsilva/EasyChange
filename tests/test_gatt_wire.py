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

@pytest.mark.parametrize("malformed", [
    b'{"action":"read","action":"write"}',
    b'{"obj":{"x":1,"x":2}}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":-Infinity}',
])
def test_gatt_wire_rejects_ambiguous_or_nonfinite_json(malformed):
    reader = FramedMessages()
    packet = len(malformed).to_bytes(4, "big") + malformed
    with pytest.raises(ValueError, match="GATT_INVALID_FRAME_JSON"):
        reader.feed(packet)
    assert reader.feed(frame_object({"ok": True})) == [{"ok": True}]


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_gatt_wire_refuses_to_encode_nonfinite_json(number):
    with pytest.raises(ValueError):
        frame_object({"value": number})
    with pytest.raises(ValueError):
        gatt_wire.seal_json(object(), {"value": number})


@pytest.mark.parametrize("malformed", [b'{"a":1,"a":2}', b'{"value":NaN}'])
def test_open_sealed_requires_unambiguous_json(malformed):
    import base64

    class DecryptedPayload:
        def decrypt(self, packet):
            return packet

    message = {"v": base64.b64encode(b"J" + malformed).decode("ascii")}
    with pytest.raises(ValueError, match="GATT_(DUPLICATE_JSON_FIELD|NONFINITE_JSON_NUMBER)"):
        gatt_wire.open_sealed(DecryptedPayload(), message)
