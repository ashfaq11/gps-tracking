"""
GT06 frame layout
-----------------
Short frames (the common case) carry a single-byte length field:

    78 78 | len(1) | proto(1) | content(..) | serial(2) | crc(2) | 0D 0A

Extended frames use a two-byte length and a different start marker:

    79 79 | len(2) | proto(1) | content(..) | serial(2) | crc(2) | 0D 0A

In both cases the length field counts every byte from the protocol number
through the CRC inclusive -- it excludes the start marker, the length field
itself, and the stop bytes.
"""

START_SHORT = b"\x78\x78"
START_LONG = b"\x79\x79"
STOP_BITS = b"\x0d\x0a"

# Bytes that follow the length field but are not content: serial(2) + crc(2).
TRAILER_LEN = 4

PROTO_LOGIN = 0x01
PROTO_LOCATION = 0x12
PROTO_HEARTBEAT = 0x13
PROTO_ALARM = 0x16
PROTO_LBS = 0x18
