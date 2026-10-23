from easychange.core.ubuntu_service import UbuntuService


def test_ubuntu_decode_does_not_misclassify_utf8_containing_nul():
    payload = b"alpha\\x00omega".replace(b"\\x00", b"\x00")
    assert UbuntuService._decode(payload) == "alpha\\x00omega".replace("\\x00", "\x00")


def test_ubuntu_decode_still_supports_utf16le_distro_names():
    assert UbuntuService._decode("Ubuntu-24.04".encode("utf-16-le")) == "Ubuntu-24.04"
    assert UbuntuService._decode("Ubuntu".encode("utf-16")) == "Ubuntu"
