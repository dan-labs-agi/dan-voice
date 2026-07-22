/**
 * streamer.h
 *
 * Owns the two hot-path FreeRTOS tasks:
 *   - Camera task (Core 0): captures frames, pushes pointers into a queue.
 *   - Network task (Core 1): pops frames, sends them over the transport,
 *     returns the buffer to the camera driver.
 *
 * Also owns a low-priority stats/monitor task that prints FPS and health
 * diagnostics once per second.
 */

#ifndef STREAMER_H
#define STREAMER_H

#include <cstdint>

namespace Streamer {

// Creates the frame queue and spawns the camera task, network task, and
// monitor task pinned to the cores configured in config.h. Call once after
// CameraModule::init() and WifiManager::begin() succeed.
void begin();

// Snapshot of live streaming statistics, safe to read from any task.
struct Stats {
    float currentFps;
    float averageFps;
    uint32_t captureTimeUs;
    uint32_t networkTimeUs;
    uint32_t queueLength;
    uint32_t droppedFrames;
    uint32_t freeHeap;
    uint32_t freePsram;
    bool clientConnected;
};

Stats getStats();

} // namespace Streamer

#endif // STREAMER_H
