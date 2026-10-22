import pytest
try:
    from app.easychange_gatt_wire import FramedMessages, frame_object, fragments
except ImportError:
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
