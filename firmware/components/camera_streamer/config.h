/**
 * config.h
 *
 * Central configuration for the ESP32-S3 (XIAO Sense) high-speed camera
 * streaming firmware. Every tunable constant lives here so the rest of
 * the codebase never hardcodes a magic number.
 */

#ifndef CONFIG_H
#define CONFIG_H

#include "esp_camera.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// ============================================================================
// Wi-Fi Configuration
// ============================================================================
#define WIFI_SSID              "LocalHost HQ"
#define WIFI_PASSWORD           "Fund3d@LocalHost#127"

// Set to 1 to force a static IP (faster reconnects, no DHCP wait).
#define WIFI_USE_STATIC_IP      0
#if WIFI_USE_STATIC_IP
    #define WIFI_STATIC_IP_ADDR      "192.168.1.50"
    #define WIFI_STATIC_GATEWAY_ADDR "192.168.1.1"
    #define WIFI_STATIC_SUBNET_ADDR  "255.255.255.0"
    #define WIFI_STATIC_DNS_ADDR     "8.8.8.8"
#endif

// Wi-Fi reconnect behavior
#define WIFI_CONNECT_TIMEOUT_MS       15000
#define WIFI_RECONNECT_RETRY_DELAY_MS 2000
#define WIFI_MAX_RETRY_DELAY_MS       10000

// ============================================================================
// TCP Server Configuration
// ============================================================================
#define TCP_SERVER_PORT          3333
#define TCP_LISTEN_BACKLOG        1      // Single client design, one at a time
#define TCP_SOCKET_SEND_TIMEOUT_S  2
#define TCP_SOCKET_SNDBUF_BYTES  (64 * 1024)
#define TCP_NODELAY_ENABLED       1      // Disable Nagle's algorithm

// ============================================================================
// Camera Pin Definitions — Seeed Studio XIAO ESP32-S3 Sense
// ============================================================================
#define CAM_PIN_PWDN     -1
#define CAM_PIN_RESET    -1
#define CAM_PIN_XCLK     10
#define CAM_PIN_SIOD     40
#define CAM_PIN_SIOC     39

#define CAM_PIN_D7       48
#define CAM_PIN_D6       11
#define CAM_PIN_D5       12
#define CAM_PIN_D4       14
#define CAM_PIN_D3       16
#define CAM_PIN_D2       18
#define CAM_PIN_D1       17
#define CAM_PIN_D0       15
#define CAM_PIN_VSYNC    38
#define CAM_PIN_HREF     47
#define CAM_PIN_PCLK     13

// ============================================================================
// Camera Defaults (runtime-configurable via camera.h API)
// ============================================================================
#define CAM_DEFAULT_FRAMESIZE     FRAMESIZE_QVGA   // 320x240
#define CAM_DEFAULT_JPEG_QUALITY  12                // 0 (best) - 63 (worst)
#define CAM_XCLK_FREQ_HZ          20000000          // 20 MHz
#define CAM_FB_COUNT              2                 // Frame buffers in PSRAM
#define CAM_GRAB_MODE             CAMERA_GRAB_LATEST
#define CAM_FB_LOCATION           CAMERA_FB_IN_PSRAM
#define CAM_PIXEL_FORMAT          PIXFORMAT_JPEG

// ============================================================================
// FreeRTOS Task Configuration
// ============================================================================
#define CAMERA_TASK_CORE          0
#define CAMERA_TASK_PRIORITY      (tskIDLE_PRIORITY + 3)
#define CAMERA_TASK_STACK_SIZE    8192

#define NETWORK_TASK_CORE         1
#define NETWORK_TASK_PRIORITY     (tskIDLE_PRIORITY + 3)
#define NETWORK_TASK_STACK_SIZE   8192

#define WIFI_MGR_TASK_CORE        1
#define WIFI_MGR_TASK_PRIORITY    (tskIDLE_PRIORITY + 2)
#define WIFI_MGR_TASK_STACK_SIZE  4096

#define MONITOR_TASK_CORE         1
#define MONITOR_TASK_PRIORITY     (tskIDLE_PRIORITY + 1)
#define MONITOR_TASK_STACK_SIZE   3072

// ============================================================================
// Frame Queue Configuration
// ============================================================================
// Depth of the inter-task queue carrying frame-buffer pointers from the
// camera task to the network task. Kept shallow on purpose: this is a
// *low-latency* pipeline, not a large buffer — we want CAMERA_GRAB_LATEST
// semantics end-to-end, not a backlog of stale frames.
#define FRAME_QUEUE_DEPTH         2

// If the network task falls behind and the queue is full, the camera task
// drops the new frame (returns fb immediately) rather than blocking, to
// guarantee the camera pipeline is never stalled by network conditions.
#define FRAME_QUEUE_SEND_TIMEOUT_MS  0   // 0 = never block, drop-on-full

// ============================================================================
// Protocol Configuration
// ============================================================================
// Wire format: [4-byte little-endian uint32 length][JPEG payload bytes]
#define PROTOCOL_LENGTH_PREFIX_BYTES  4
#define PROTOCOL_MAX_FRAME_BYTES      (400 * 1024)  // Sanity ceiling

// ============================================================================
// Diagnostics / Monitoring
// ============================================================================
#define STATS_REPORT_INTERVAL_MS   1000

// ============================================================================
// Misc
// ============================================================================
#define LED_BUILTIN_PIN            21     // XIAO ESP32-S3 user LED (active-low)

#endif // CONFIG_H
