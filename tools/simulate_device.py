"""
Fake GT06 tracker -- exercises the gateway without real hardware.

    python tools/simulate_device.py --host 127.0.0.1 --port 5023 --pings 5
    python tools/simulate_device.py --protocol 0x22   # newer Concox, ACC byte
    python tools/simulate_device.py --acc off --hold 120   # parked, answers commands

Sends a login, then location frames with the latitude drifting north each
ping (simulated movement), with a heartbeat in between. Frames are built by
the same code path the gateway decodes, including real CRCs.

Like a PT06, it answers server commands (engine cut-off "RELAY,1#", restore
"RELAY,0#") with a 0x15 reply carrying the command's server flag. --hold
keeps it online (heartbeating) afterwards so a command can reach it.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gps_gateway.protocol import (  # noqa: E402
    PROTO_COMMAND,
    PROTO_COMMAND_REPLY,
    PROTO_HEARTBEAT,
    PROTO_LOCATION,
    PROTO_LOCATION_ACC,
    PROTO_LOGIN,
    build_frame,
)
from gps_gateway.protocol.framing import FrameDecoder  # noqa: E402

# Bit 10 = northern hemisphere, bit 11 clear = eastern, bit 12 = GPS fixed.
FLAGS_N_E_FIXED = 0x1400
_COORD_SCALE = 30000.0 * 60.0
# MCC 404 (India), MNC 45, LAC 0x1005, CellID 0x005483.
SERVING_CELL = bytes.fromhex("0194" "2D" "1005" "005483")
# Terminal info 0x46 (GPS tracking, charging, ACC on), voltage 4, GSM 4.
HEARTBEAT_ACC_ON = bytes.fromhex("46" "04" "04" "0001")
# Terminal info 0x44: GPS tracking, charging, ACC off.
HEARTBEAT_ACC_OFF = bytes.fromhex("44" "04" "04" "0001")
# 0x22 trailer: ACC on, upload reason 0x00 (timed), real-time (not re-upload).
ACC_TRAILER_ON = bytes.fromhex("01" "00" "00")
ACC_TRAILER_OFF = bytes.fromhex("00" "00" "00")

# What a PT06 answers, per command.
COMMAND_REPLIES = {
    "RELAY,1#": "Cut off the fuel supply: Success!",
    "RELAY,0#": "Restore fuel supply: Success!",
}


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
        + SERVING_CELL
    )


async def listen(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Print every ACK, and answer every command the way a PT06 does."""
    decoder = FrameDecoder()
    while True:
        data = await reader.read(1024)
        if not data:
            print("  server closed the connection")
            return
        for frame in decoder.feed(data):
            if frame.protocol == PROTO_COMMAND:
                flag = frame.content[1:5]
                command = frame.content[5 : 1 + frame.content[0]].decode("ascii", "replace")
                answer = COMMAND_REPLIES.get(command, f"Unknown command: {command}").encode()
                print(f"  COMMAND {command!r} (flag {int.from_bytes(flag, 'big')}) -> {answer.decode()!r}")
                writer.write(
                    build_frame(PROTO_COMMAND_REPLY, frame.serial, bytes([4 + len(answer)]) + flag + answer)
                )
                await writer.drain()
            else:
                status = "crc ok" if frame.crc_ok else "BAD CRC"
                print(f"  ACK proto=0x{frame.protocol:02X} serial={frame.serial} ({status})")


async def simulate(
    host: str,
    port: int,
    imei: str,
    pings: int,
    interval: float,
    protocol: int,
    acc_on: bool = True,
    hold: float = 0.0,
) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    print(f"Connected to {host}:{port} as IMEI {imei}")
    listener = asyncio.create_task(listen(reader, writer))
    heartbeat = HEARTBEAT_ACC_ON if acc_on else HEARTBEAT_ACC_OFF
    serial = 1
    try:
        terminal_id = bytes.fromhex(imei.zfill(16))
        writer.write(build_frame(PROTO_LOGIN, serial, terminal_id))
        await writer.drain()
        print("Sent login")
        await asyncio.sleep(0.3)

        lat, lon = 12.971598, 77.594566  # Bengaluru
        for i in range(pings):
            serial += 1
            lat += 0.0009  # drift roughly 100 m north per ping
            content = location_content(lat, lon, speed_kmh=42 if acc_on else 0, course_deg=15)
            if protocol == PROTO_LOCATION_ACC:
                content += ACC_TRAILER_ON if acc_on else ACC_TRAILER_OFF
            writer.write(build_frame(protocol, serial, content))
            await writer.drain()
            print(f"Sent location {i + 1}/{pings}: {lat:.6f}, {lon:.6f}")
            await asyncio.sleep(0.3)

            if i % 2 == 1 or not acc_on:
                serial += 1
                writer.write(build_frame(PROTO_HEARTBEAT, serial, heartbeat))
                await writer.drain()
                print(f"Sent heartbeat (ACC {'on' if acc_on else 'off'})")
                await asyncio.sleep(0.3)

            if i < pings - 1:
                await asyncio.sleep(interval)

        # Stay online, heartbeating, so a queued command can reach us.
        deadline = asyncio.get_running_loop().time() + hold
        while asyncio.get_running_loop().time() < deadline and not listener.done():
            await asyncio.sleep(min(5.0, max(0.0, deadline - asyncio.get_running_loop().time())))
            serial += 1
            writer.write(build_frame(PROTO_HEARTBEAT, serial, heartbeat))
            await writer.drain()
    finally:
        listener.cancel()
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
    parser.add_argument(
        "--protocol",
        type=lambda v: int(v, 0),
        choices=(PROTO_LOCATION, PROTO_LOCATION_ACC),
        default=PROTO_LOCATION,
        metavar="{0x12,0x22}",
        help="location packet type: 0x12 (classic GT06) or 0x22 (with ACC byte)",
    )
    parser.add_argument(
        "--acc", choices=("on", "off"), default="on", help="ignition to report (default on)"
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="seconds to stay online afterwards, heartbeating and answering commands",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(
            simulate(
                args.host,
                args.port,
                args.imei,
                args.pings,
                args.interval,
                args.protocol,
                acc_on=args.acc == "on",
                hold=args.hold,
            )
        )
    except KeyboardInterrupt:
        pass
    except ConnectionRefusedError:
        print(f"Could not connect to {args.host}:{args.port} -- is the gateway running?")
        raise SystemExit(1)
