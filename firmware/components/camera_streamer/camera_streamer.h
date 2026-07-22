#ifndef CAMERA_STREAMER_H
#define CAMERA_STREAMER_H

#ifdef __cplusplus
extern "C" {
#endif

/* Starts camera init -> WifiManager::begin() -> Streamer::begin() on its
 * own FreeRTOS task. Non-blocking: returns immediately after spawning the
 * task, so it can be called from an app_main() that also owns other
 * subsystems (e.g. NimBLE) without delaying their startup. */
void camera_streamer_start(void);

#ifdef __cplusplus
}
#endif

#endif // CAMERA_STREAMER_H
