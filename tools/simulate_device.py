"""
Fake GT06 tracker -- exercises the gateway without real hardware.

    python tools/simulate_device.py --host 127.0.0.1 --port 5023 --pings 5

Sends a login, then location frames with the latitude drifting north each
ping (simulated movement), with a heartbeat in between. Frames are built by
the same code path the gateway decodes, including real CRCs.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gps_gateway.protocol import (  # noqa: E402
    PROTO_HEARTBEAT,
    PROTO_LOCATION,
    PROTO_LOGIN,
    build_frame,
)
from gps_gateway.protocol.framing import FrameDecoder  # noqa: E402

# Bit 10 = northern hemisphere, bit 11 clear = eastern, bit 12 = GPS fixed.
FLAGS_N_E_FIXED = 0x1400
_COORD_SCALE = 30000.0 * 60.0


def location_content(lat: float, lon: float, speed_kmh: int, course_deg: int) -> bytes:
    now = datetime.now(timezone.utc)
    stamp = bytes([now.year % 100, now.month, now.day, now.hour, now.minute, now.second])
    flags = FLAGS_N_E_FIXED | (course_deg & 0x03FF)
    return (
        stamp
        + bytes([0xC9])  # GPS info length 12, 9 satellites
        + int(abs(lat) * _COORD_SCALE).to_bytes(4, "big")
        + int(abs(lon) * _COORD_SCALE).to_bytes(4, "big")
        + bytes([speed_kmh & 0xFF])
        + flags.to_bytes(2, "big")
    )


async def read_ack(reader: asyncio.StreamReader, label: str) -> None:
    try:
        data = await asyncio.wait_for(reader.read(64), timeout=5)
    except asyncio.TimeoutError:
        print(f"  {label}: no ACK (timeout)")
        return
    if not data:
        print(f"  {label}: server closed the connection")
        return
    for frame in FrameDecoder().feed(data):
        status = "crc ok" if frame.crc_ok else "BAD CRC"
        print(f"  {label}: ACK proto=0x{frame.protocol:02X} serial={frame.serial} ({status})")


async def simulate(host: str, port: int, imei: str, pings: int, interval: float) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    print(f"Connected to {host}:{port} as IMEI {imei}")
    serial = 1
    try:
        terminal_id = bytes.fromhex(imei.zfill(16))
        writer.write(build_frame(PROTO_LOGIN, serial, terminal_id))
        await writer.drain()
        print("Sent login")
        await read_ack(reader, "login")

        lat, lon = 12.971598, 77.594566  # Bengaluru
        for i in range(pings):
            serial += 1
            lat += 0.0009  # drift roughly 100 m north per ping
            content = location_content(lat, lon, speed_kmh=42, course_deg=15)
            writer.write(build_frame(PROTO_LOCATION, serial, content))
            await writer.drain()
            print(f"Sent location {i + 1}/{pings}: {lat:.6f}, {lon:.6f}")
            await read_ack(reader, "location")

            if i % 2 == 1:
                serial += 1
                writer.write(build_frame(PROTO_HEARTBEAT, serial, b"\x04\x00\x01\x00\x01"))
                await writer.drain()
                print("Sent heartbeat")
                await read_ack(reader, "heartbeat")

            if i < pings - 1:
                await asyncio.sleep(interval)
    finally:
        writer.close()
        await writer.wait_closed()
        print("Disconnected")


def parse_args():
    parser = argparse.ArgumentParser(description="Simulate a GT06 GPS tracker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5023)
    parser.add_argument("--imei", default="868120303372449")
    parser.add_argument("--pings", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between pings")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(simulate(args.host, args.port, args.imei, args.pings, args.interval))
    except KeyboardInterrupt:
        pass
    except ConnectionRefusedError:
        print(f"Could not connect to {args.host}:{args.port} -- is the gateway running?")
        raise SystemExit(1)
