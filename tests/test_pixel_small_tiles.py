"""ECP1 HDMI decoding when a QR/PIXEL tile's white quiet-zone merges
with a light EasyChange theme, preserving CRC and all 32 independent cells.
Only local synthetic frames are used; no capture card or remote PC.
"""
import json

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from easychange.remote.pixel_transport import (
    SIDE, encode_pixel_bitmap, decode_pixel_frame,
)


@pytest.mark.parametrize("size", [110, 125, 144])
def test_ecp1_white_background_restores_registration_border(size):
    wire=json.dumps({"p":"EC2","s":"QSMALLPIXEL","n":4,"i":2,"d":"TEST"},
                    separators=(",",":"))
    raw=np.frombuffer(encode_pixel_bitmap(wire),dtype=np.uint8).reshape(SIDE,SIDE)
    frame=np.full((720,1280,3),255,dtype=np.uint8)
    tile=cv2.resize(raw,(size,size),interpolation=cv2.INTER_NEAREST)
    frame[90:90+size,205:205+size,:]=tile[:,:,None]
    assert wire in decode_pixel_frame(frame)


def test_ecp1_32_tiles_on_white_background_all_crc_verified():
    frame=np.full((720,1280,3),255,dtype=np.uint8)
    expected=[]
    for index in range(32):
        wire=json.dumps({"p":"EC2","s":"Q32PIXELTEST","n":32,
                         "i":index,"d":f"{index:02d}"},
                        separators=(",",":"))
        expected.append(wire)
        raw=np.frombuffer(encode_pixel_bitmap(wire),dtype=np.uint8).reshape(SIDE,SIDE)
        tile=cv2.resize(raw,(125,125),interpolation=cv2.INTER_NEAREST)
        y=55+(index//8)*144
        x=75+(index%8)*145
        frame[y:y+125,x:x+125,:]=tile[:,:,None]
    observed=decode_pixel_frame(frame)
    assert len(observed)==32
    assert set(observed)==set(expected)
