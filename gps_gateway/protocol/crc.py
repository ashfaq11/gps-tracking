"""CRC16-ITU (X.25) as used by the GT06 protocol."""

_POLY = 0x8408  # 0x1021 reflected
_TABLE = []
for _byte in range(256):
    _crc = _byte
    for _ in range(8):
        _crc = (_crc >> 1) ^ _POLY if _crc & 1 else _crc >> 1
    _TABLE.append(_crc)


def crc16_itu(data: bytes) -> int:
    """
    Checksum covering the length field through the serial number, i.e.
    everything between the start marker and the CRC itself.
    """
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFF
