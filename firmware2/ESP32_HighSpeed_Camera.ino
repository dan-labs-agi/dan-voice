/**
 * ESP32_HighSpeed_Camera.ino
 *
 * Entry point. Responsible only for orchestration:
 *   1. Bring up serial diagnostics.
 *   2. Initialize the camera (camera.h).
 *   3. Bring up Wi-Fi and its auto-reconnect monitor (wifi_manager.h).
 *   4. Start the capture/network/monitor tasks (streamer.h).
 *
 * All actual logic lives in the other modules — this file stays thin by
 * design so the architecture remains easy to reason about and extend.
 */

#include <Arduino.h>
#include "config.h"
#include "camera.h"
#include "wifi_manager.h"
#include "streamer.h"

static void blinkError(int times) {
    pinMode(LED_BUILTIN_PIN, OUTPUT);
    for (int i = 0; i < times; i++) {
        digitalWrite(LED_BUILTIN_PIN, LOW);  // active-low LED on
        delay(150);
        digitalWrite(LED_BUILTIN_PIN, HIGH); // off
        delay(150);
    }
}

void setup() {
    Serial.begin(SERIAL_BAUD_RATE);
    delay(300); // let USB CDC enumerate before we start printing
    Serial.println();
    Serial.println("=================================================");
    Serial.println(" ESP32-S3 XIAO Sense — High-Speed Camera Streamer");
    Serial.println("=================================================");

    Serial.printf("[Setup] Free heap: %u bytes\n", (unsigned)ESP.getFreeHeap());
    Serial.printf("[Setup] Free PSRAM: %u bytes\n", (unsigned)ESP.getFreePsram());
    Serial.printf("[Setup] PSRAM found: %s\n", psramFound() ? "yes" : "NO (degraded mode)");

    // --- Camera init, with a few retries since sensor bring-up can be
    // flaky immediately after power-on. ---
    CameraStatus camStatus = CameraStatus::ERR_INIT_FAILED;
    for (int attempt = 1; attempt <= 3 && camStatus != CameraStatus::OK; attempt++) {
        Serial.printf("[Setup] Camera init attempt %d/3...\n", attempt);
        camStatus = CameraModule::init();
        if (camStatus != CameraStatus::OK) {
            Serial.printf("[Setup] Camera init failed: %s\n", CameraModule::statusToString(camStatus));
            delay(500);
        }
    }

    if (camStatus != CameraStatus::OK) {
        Serial.println("[Setup] FATAL: camera failed to initialize after 3 attempts.");
        Serial.println("[Setup] Halting. Check camera ribbon connection and power.");
        while (true) {
            blinkError(2);
            delay(1000);
        }
    }

    // --- Wi-Fi init. begin() blocks for the initial connect but then hands
    // off reconnect duty to its own background task, so this call returning
    // false (e.g. AP temporarily unreachable) is not fatal — the monitor
    // task will keep retrying and the network task will simply wait. ---
    bool wifiOk = WifiManager::begin();
    if (!wifiOk) {
        Serial.println("[Setup] WARNING: initial Wi-Fi connect failed; background retry is active.");
    } else {
        Serial.printf("[Setup] Wi-Fi connected. IP=%s\n", WifiManager::localIP().c_str());
        Serial.printf("[Setup] Connect with: nc/python client -> %s:%d\n",
                      WifiManager::localIP().c_str(), TCP_SERVER_PORT);
    }

    // --- Start the capture/network/monitor pipeline. ---
    Streamer::begin();

    Serial.println("[Setup] Initialization complete. Streaming pipeline is live.");
}

void loop() {
    // All real work happens in the FreeRTOS tasks started by Streamer::begin().
    // loop() itself just idles; keeping it lightweight avoids starving the
    // Arduino loop task's own low priority slot and any background Wi-Fi
    // event processing that runs on it.
    vTaskDelay(pdMS_TO_TICKS(1000));
}
