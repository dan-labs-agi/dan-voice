/**
 * streamer.cpp
 *
 * Implements the producer/consumer pipeline:
 *
 *   Core 0                          Core 1
 *   ------                          ------
 *   cameraTask()                    networkTask()
 *     capture frame       --queue-->  wait for client
 *     push fb* to queue                pop fb*
 *     (never blocks on net)            send over transport
 *                                      return fb* to driver
 *
 *   Core 1 (low priority)
 *   ---------------------
 *   monitorTask()
 *     print FPS / heap / PSRAM / queue depth once per second
 *
 * Design notes:
 *  - The queue carries POINTERS (camera_fb_t*), never frame data itself,
 *    so there is exactly one copy of each JPEG in memory at a time (the
 *    buffer inside the driver's frame buffer pool).
 *  - If the queue is full (network task falling behind), the camera task
 *    drops the new frame immediately — it must never block, or the sensor's
 *    own internal buffering overflows (the FB-OVF condition this firmware
 *    is designed to eliminate).
 *  - The network task returns every frame buffer it pops, whether the send
 *    succeeded or not, so the driver's buffer pool never starves.
 */

#include "streamer.h"
#include "camera.h"
#include "wifi_manager.h"
#include "protocol.h"
#include "config.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

// Declared in protocol.cpp — factory for the concrete transport. This is
// the only place that needs to know a TCP transport exists; everything
// else here talks to ITransport*.
extern ITransport* createTcpTransport();

namespace {

const char* TAG = "streamer";

inline uint32_t millis() {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

inline uint32_t micros() {
    return (uint32_t)esp_timer_get_time();
}

QueueHandle_t s_frameQueue = nullptr;
ITransport* s_transport = nullptr;

// ---- Shared stats, protected by a spinlock (cheap, ISR-safe enough for
// our purposes, and avoids a full mutex for tiny read/write critical
// sections that run every frame). ----
portMUX_TYPE s_statsMux = portMUX_INITIALIZER_UNLOCKED;

struct SharedStats {
    uint32_t framesThisSecond = 0;
    uint32_t framesTotal = 0;
    uint32_t droppedFrames = 0;
    uint32_t lastCaptureTimeUs = 0;
    uint32_t lastNetworkTimeUs = 0;
    float currentFps = 0.0f;
    float averageFps = 0.0f;
    bool clientConnected = false;
} s_stats;

uint32_t s_bootMillis = 0;

// ----------------------------------------------------------------------------
// Camera Task — Core 0
// ----------------------------------------------------------------------------
void cameraTask(void* pvParameters) {
    ESP_LOGI(TAG, "[CameraTask] Started on core %d", xPortGetCoreID());

    for (;;) {
        uint32_t t0 = micros();
        camera_fb_t* fb = CameraModule::capture();
        uint32_t captureTimeUs = micros() - t0;

        if (fb == nullptr) {
            // Capture failure: back off briefly to avoid a hot error loop,
            // then retry. This does not touch the queue or the network task.
            portENTER_CRITICAL(&s_statsMux);
            s_stats.droppedFrames++;
            portEXIT_CRITICAL(&s_statsMux);
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        portENTER_CRITICAL(&s_statsMux);
        s_stats.lastCaptureTimeUs = captureTimeUs;
        portEXIT_CRITICAL(&s_statsMux);

        // Non-blocking send: if the network task hasn't drained the queue
        // (client slow/disconnected), drop this frame and return the buffer
        // immediately rather than stalling capture.
        BaseType_t queued = xQueueSend(s_frameQueue, &fb, pdMS_TO_TICKS(FRAME_QUEUE_SEND_TIMEOUT_MS));
        if (queued != pdPASS) {
            CameraModule::release(fb);
            portENTER_CRITICAL(&s_statsMux);
            s_stats.droppedFrames++;
            portEXIT_CRITICAL(&s_statsMux);
        }
        // NOTE: no vTaskDelay here — capture cadence is governed by the
        // sensor/DMA pipeline itself (esp_camera_fb_get blocks until a
        // frame is ready), so we always run at the sensor's true frame rate.
    }
}

// ----------------------------------------------------------------------------
// Network Task — Core 1
// ----------------------------------------------------------------------------
void networkTask(void* pvParameters) {
    ESP_LOGI(TAG, "[NetworkTask] Started on core %d", xPortGetCoreID());

    s_transport = createTcpTransport();
    if (!s_transport->begin()) {
        ESP_LOGE(TAG, "[NetworkTask] FATAL: transport begin() failed, retrying in loop");
    }

    for (;;) {
        // Wi-Fi must be up before we even try to accept/send.
        if (!WifiManager::isConnected()) {
            portENTER_CRITICAL(&s_statsMux);
            s_stats.clientConnected = false;
            portEXIT_CRITICAL(&s_statsMux);
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        if (!s_transport->isClientConnected()) {
            portENTER_CRITICAL(&s_statsMux);
            s_stats.clientConnected = false;
            portEXIT_CRITICAL(&s_statsMux);

            bool got = s_transport->waitForClient(200); // poll, don't block forever
            if (!got) {
                // Drain and drop any frames queued while we had no client,
                // so we don't send a burst of stale frames the moment one
                // connects.
                camera_fb_t* stale = nullptr;
                while (xQueueReceive(s_frameQueue, &stale, 0) == pdPASS) {
                    CameraModule::release(stale);
                    portENTER_CRITICAL(&s_statsMux);
                    s_stats.droppedFrames++;
                    portEXIT_CRITICAL(&s_statsMux);
                }
                continue;
            }
        }

        portENTER_CRITICAL(&s_statsMux);
        s_stats.clientConnected = true;
        portEXIT_CRITICAL(&s_statsMux);

        camera_fb_t* fb = nullptr;
        if (xQueueReceive(s_frameQueue, &fb, pdMS_TO_TICKS(200)) != pdPASS) {
            continue; // no frame ready yet, loop and re-check connection state
        }

        uint32_t t0 = micros();
        bool ok = Protocol::sendFrame(*s_transport, fb->buf, fb->len);
        uint32_t networkTimeUs = micros() - t0;

        CameraModule::release(fb);

        portENTER_CRITICAL(&s_statsMux);
        s_stats.lastNetworkTimeUs = networkTimeUs;
        if (ok) {
            s_stats.framesThisSecond++;
            s_stats.framesTotal++;
        } else {
            s_stats.droppedFrames++;
            s_stats.clientConnected = false;
        }
        portEXIT_CRITICAL(&s_statsMux);
    }
}

// ----------------------------------------------------------------------------
// Monitor Task — Core 1, low priority
// ----------------------------------------------------------------------------
void monitorTask(void* pvParameters) {
    ESP_LOGI(TAG, "[MonitorTask] Started on core %d", xPortGetCoreID());

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(STATS_REPORT_INTERVAL_MS));

        uint32_t framesThisSecond, framesTotal, dropped, captureUs, networkUs;
        bool clientConnected;

        portENTER_CRITICAL(&s_statsMux);
        framesThisSecond = s_stats.framesThisSecond;
        framesTotal      = s_stats.framesTotal;
        dropped          = s_stats.droppedFrames;
        captureUs        = s_stats.lastCaptureTimeUs;
        networkUs        = s_stats.lastNetworkTimeUs;
        clientConnected  = s_stats.clientConnected;
        s_stats.currentFps = (float)framesThisSecond;
        s_stats.framesThisSecond = 0;
        portEXIT_CRITICAL(&s_statsMux);

        float elapsedSec = (millis() - s_bootMillis) / 1000.0f;
        float avgFps = elapsedSec > 0 ? (framesTotal / elapsedSec) : 0.0f;

        portENTER_CRITICAL(&s_statsMux);
        s_stats.averageFps = avgFps;
        portEXIT_CRITICAL(&s_statsMux);

        UBaseType_t queueWaiting = uxQueueMessagesWaiting(s_frameQueue);

        ESP_LOGI(TAG,
            "FPS(cur=%.1f avg=%.1f) cap=%uus net=%uus queue=%u dropped=%u "
            "heap=%u psram=%u client=%s",
            (float)framesThisSecond, avgFps,
            captureUs, networkUs,
            (unsigned)queueWaiting, dropped,
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
            clientConnected ? "yes" : "no"
        );
    }
}

} // namespace

namespace Streamer {

void begin() {
    s_bootMillis = millis();

    s_frameQueue = xQueueCreate(FRAME_QUEUE_DEPTH, sizeof(camera_fb_t*));
    if (s_frameQueue == nullptr) {
        ESP_LOGE(TAG, "FATAL: failed to create frame queue");
        return;
    }

    xTaskCreatePinnedToCore(
        cameraTask, "camera_task", CAMERA_TASK_STACK_SIZE,
        nullptr, CAMERA_TASK_PRIORITY, nullptr, CAMERA_TASK_CORE
    );

    xTaskCreatePinnedToCore(
        networkTask, "network_task", NETWORK_TASK_STACK_SIZE,
        nullptr, NETWORK_TASK_PRIORITY, nullptr, NETWORK_TASK_CORE
    );

    xTaskCreatePinnedToCore(
        monitorTask, "monitor_task", MONITOR_TASK_STACK_SIZE,
        nullptr, MONITOR_TASK_PRIORITY, nullptr, MONITOR_TASK_CORE
    );

    ESP_LOGI(TAG, "All tasks started");
}

Stats getStats() {
    Stats out{};
    portENTER_CRITICAL(&s_statsMux);
    out.currentFps      = s_stats.currentFps;
    out.averageFps       = s_stats.averageFps;
    out.captureTimeUs    = s_stats.lastCaptureTimeUs;
    out.networkTimeUs    = s_stats.lastNetworkTimeUs;
    out.droppedFrames    = s_stats.droppedFrames;
    out.clientConnected  = s_stats.clientConnected;
    portEXIT_CRITICAL(&s_statsMux);

    out.queueLength = s_frameQueue ? uxQueueMessagesWaiting(s_frameQueue) : 0;
    out.freeHeap    = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    out.freePsram   = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    return out;
}

} // namespace Streamer
