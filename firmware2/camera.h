/**
 * camera.h
 *
 * Thin, well-defined wrapper around the esp32-camera driver.
 * Responsible for:
 *   - Initializing the OV3660 sensor with performance-oriented settings
 *   - Exposing a simple capture() call that returns a frame buffer
 *   - Exposing runtime setters for resolution / JPEG quality
 *   - Returning frame buffers back to the driver once consumed
 *
 * This module does NOT know anything about FreeRTOS tasks, queues, or
 * networking. It is a pure hardware abstraction layer.
 */

#ifndef CAMERA_MODULE_H
#define CAMERA_MODULE_H

#include <Arduino.h>
#include "esp_camera.h"

// Result codes for camera operations.
enum class CameraStatus {
    OK = 0,
    ERR_INIT_FAILED,
    ERR_SENSOR_NOT_FOUND,
    ERR_CAPTURE_FAILED,
    ERR_INVALID_PARAM
};

namespace CameraModule {

// Initializes the camera peripheral with the pin mapping and defaults
// defined in config.h. Must be called exactly once before any other
// CameraModule function. Safe to retry on failure.
CameraStatus init();

// Captures a single frame. Returns nullptr on failure (caller must check).
// The returned camera_fb_t* MUST be released via release() once the caller
// (network task) is done reading its buffer/len.
camera_fb_t* capture();

// Releases a frame buffer previously returned by capture(). Must be called
// exactly once per successful capture() to avoid PSRAM fragmentation /
// frame-buffer starvation.
void release(camera_fb_t* fb);

// Runtime reconfiguration. Thread-safe with respect to capture() as long as
// callers do not invoke these mid-capture from another task without a lock;
// in this architecture only the main setup path and a future control
// channel would call these, never the hot capture loop.
CameraStatus setFrameSize(framesize_t size);
CameraStatus setJpegQuality(int quality); // 0 (best) .. 63 (worst)

// Returns true once init() has succeeded.
bool isInitialized();

// Returns a human-readable string for a CameraStatus, for logging.
const char* statusToString(CameraStatus status);

} // namespace CameraModule

#endif // CAMERA_MODULE_H
