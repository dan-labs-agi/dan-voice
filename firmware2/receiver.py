#!/usr/bin/env python3
"""
receiver.py

Laptop-side receiver for the ESP32-S3 XIAO Sense high-speed camera stream.

Wire protocol (matches protocol.h/.cpp on the firmware):
    repeat forever:
        4 bytes  little-endian uint32   frame length in bytes
        N bytes                          JPEG payload

Usage:
    python receiver.py --host 192.168.1.42 --port 3333

Dependencies:
    pip install opencv-python numpy
"""

import argparse
import socket
import struct
import time
import sys

import numpy as np
import cv2

HEADER_SIZE = 4  # bytes, matches PROTOCOL_LENGTH_PREFIX_BYTES
MAX_FRAME_BYTES = 400 * 1024  # matches PROTOCOL_MAX_FRAME_BYTES, sanity ceiling

RECONNECT_DELAY_SEC = 2.0
SOCKET_TIMEOUT_SEC = 5.0


def recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly num_bytes from the socket, or raise ConnectionError."""
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed by peer")
        buf.extend(chunk)
    return bytes(buf)


def connect(host: str, port: int) -> socket.socket:
    print(f"[Receiver] Connecting to {host}:{port} ...")
    sock = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT_SEC)
    sock.settimeout(SOCKET_TIMEOUT_SEC)
    # Match the firmware's low-latency intent on the receive side too.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("[Receiver] Connected.")
    return sock


def run(host: str, port: int, window_name: str = "ESP32-S3 Stream") -> None:
    fps_counter = 0
    fps_timer = time.time()
    display_fps = 0.0

    while True:
        try:
            sock = connect(host, port)
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            print(f"[Receiver] Connect failed ({exc}); retrying in {RECONNECT_DELAY_SEC}s")
            time.sleep(RECONNECT_DELAY_SEC)
            continue

        try:
            while True:
                header = recv_exact(sock, HEADER_SIZE)
                frame_len = struct.unpack("<I", header)[0]

                if frame_len == 0 or frame_len > MAX_FRAME_BYTES:
                    raise ValueError(f"Implausible frame length {frame_len}, resyncing connection")

                jpeg_bytes = recv_exact(sock, frame_len)

                frame = cv2.imdecode(
                    np.frombuffer(jpeg_bytes, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if frame is None:
                    print("[Receiver] Warning: failed to decode a frame, skipping")
                    continue

                fps_counter += 1
                now = time.time()
                if now - fps_timer >= 1.0:
                    display_fps = fps_counter / (now - fps_timer)
                    fps_counter = 0
                    fps_timer = now

                cv2.putText(
                    frame,
                    f"FPS: {display_fps:.1f}",
                    (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2,
                )

                cv2.imshow(window_name, frame)

                # This also hands control back to OpenCV's event loop so the
                # window stays responsive; 'q' quits cleanly.
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    sock.close()
                    cv2.destroyAllWindows()
                    return

        except (ConnectionError, socket.timeout, ValueError, OSError) as exc:
            print(f"[Receiver] Connection lost ({exc}); reconnecting...")
            try:
                sock.close()
            except OSError:
                pass
            time.sleep(RECONNECT_DELAY_SEC)
            continue


def main():
    parser = argparse.ArgumentParser(description="ESP32-S3 high-speed camera stream receiver")
    parser.add_argument("--host", required=True, help="ESP32 IP address, e.g. 192.168.1.42")
    parser.add_argument("--port", type=int, default=3333, help="TCP port (default: 3333)")
    args = parser.parse_args()

    try:
        run(args.host, args.port)
    except KeyboardInterrupt:
        print("\n[Receiver] Interrupted, shutting down.")
        cv2.destroyAllWindows()
        sys.exit(0)


if __name__ == "__main__":
    main()
