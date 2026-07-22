# ESP32-S3 XIAO Sense — High-Speed Camera Streamer

Production-oriented firmware that turns the Seeed Studio XIAO ESP32-S3 Sense
into a pure image-acquisition device: capture → JPEG compress → stream over
TCP. No AI/CV work runs on the device — all of that happens on the laptop
receiver.

## Project layout

```
ESP32_HighSpeed_Camera/
├── ESP32_HighSpeed_Camera.ino   # setup()/loop() orchestration only
├── config.h                    # every tunable constant lives here
├── camera.h / camera.cpp       # OV3660 hardware abstraction
├── wifi_manager.h / .cpp       # connect + auto-reconnect (own FreeRTOS task)
├── protocol.h / .cpp           # wire format + ITransport + TcpTransport
├── streamer.h / .cpp           # camera task (core 0), network task (core 1),
│                                # monitor task, frame queue
└── receiver.py                 # laptop-side Python/OpenCV viewer
```

## Arduino IDE / arduino-cli setup

1. Board package: **esp32 by Espressif Systems**, version 2.0.14+ (or the
   current 3.x core — API used here is stable across both).
2. Board: **XIAO_ESP32S3** (Tools → Board → esp32 → XIAO_ESP32S3).
3. Required Tools menu settings:
   - **PSRAM:** `OPI PSRAM` — mandatory, the frame buffers live here.
   - **Partition Scheme:** `Default 8MB with spiffs` (or any scheme leaving
     enough app space; camera + Wi-Fi + FreeRTOS is a sizable image).
   - **CPU Frequency:** `240MHz (WiFi/BT)`.
   - **USB CDC On Boot:** `Enabled` (so `Serial` diagnostics show up over
     the native USB port immediately).
4. Library: **esp32-camera** — bundled with the esp32 Arduino core, no
   separate install needed. Do not install the standalone `esp32cam`
   library; this project calls `esp_camera.h` directly.

## Before flashing

Edit `config.h`:

```cpp
#define WIFI_SSID     "your_ssid"
#define WIFI_PASSWORD "your_password"
```

Everything else (resolution, JPEG quality, task priorities, queue depth,
port number) is also in `config.h` if you want to tune it — the rest of the
codebase never hardcodes these values.

## Flashing

Put the XIAO ESP32-S3 Sense into a stable USB connection, select the correct
serial port, and upload as usual. On first boot, open the Serial Monitor at
115200 baud — the firmware prints its IP address and the port to connect to:

```
[Setup] Wi-Fi connected. IP=192.168.1.42
[Setup] Connect with: nc/python client -> 192.168.1.42:3333
```

## Running the receiver

```bash
pip install opencv-python numpy
python receiver.py --host 192.168.1.42 --port 3333
```

Press `q` in the video window to quit. The receiver reconnects automatically
if the ESP32 reboots or Wi-Fi drops.

## Why this design eliminates FB-OVF

`cam_hal: FB-OVF` happens when the sensor's DMA pipeline produces frames
faster than something downstream drains them, and the driver's internal
frame-buffer pool fills up. Two things in this firmware prevent that:

- **`CAMERA_GRAB_LATEST`** tells the driver to always keep the *newest*
  frame available rather than queuing every frame it captures — so a slow
  consumer doesn't back up the DMA path itself.
- **The camera task never blocks on the network.** It pushes a frame
  pointer into a 2-deep FreeRTOS queue with a zero-timeout send. If the
  network task hasn't drained the queue (client slow, disconnected, or
  temporarily congested), the new frame is dropped and its buffer is
  returned to the driver immediately — capture cadence is never gated on
  Wi-Fi throughput.

This trades a dropped frame now and then (visible in the `dropped=` stat)
for guaranteed pipeline stability — which matches the project's stated
priority of "no frame corruption / no watchdog resets / stable for hours"
over "never drop a single frame."

## Realistic performance expectations

The requested targets (QVGA 25–30 FPS, VGA 12–18 FPS) are achievable on a
clean 2.4GHz network with reasonable signal strength and a `JPEG_QUALITY`
around 10–15, but they are network- and RF-environment-dependent, not a
guarantee this firmware can enforce by itself — the monitor task's
`net=` timing and `dropped=` counter are there so you can see, in real
time, whether you're capture-bound or network-bound and tune
`CAM_DEFAULT_JPEG_QUALITY` / `CAM_DEFAULT_FRAMESIZE` accordingly.

## Extending to UDP or USB later

`protocol.h` defines transport behavior as the abstract `ITransport`
interface; `streamer.cpp` only ever holds an `ITransport*` obtained from
`createTcpTransport()`. Adding UDP or a USB-CDC transport means:

1. Implement a new class satisfying `ITransport` (e.g. `UdpTransport`) in
   `protocol.cpp`.
2. Add a `createUdpTransport()` factory function next to
   `createTcpTransport()`.
3. Swap the factory call in `streamer.cpp`'s `networkTask()` (or make it
   config-selectable via a `#define` in `config.h`).

No changes are needed to `camera.cpp`, the frame queue, or the monitor task.

## Diagnostics reference

Printed once per second by the monitor task:

| Field   | Meaning                                              |
|---------|-------------------------------------------------------|
| `cur`   | Frames actually sent in the last second                |
| `avg`   | Frames sent per second since boot                       |
| `cap`   | Time (µs) the last `esp_camera_fb_get()` call took       |
| `net`   | Time (µs) the last `Protocol::sendFrame()` call took     |
| `queue` | Frames currently sitting in the inter-task queue          |
| `dropped` | Total frames dropped since boot (queue-full or send failure) |
| `heap`  | Free internal heap, bytes                                |
| `psram` | Free PSRAM, bytes                                        |
| `client`| Whether a TCP client is currently connected                |
