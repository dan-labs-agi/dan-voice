/**
 * wifi_manager.cpp
 *
 * Implementation of Wi-Fi lifecycle management.
 */

#include "wifi_manager.h"
#include "config.h"
#include <WiFi.h>

namespace {
    volatile bool s_connected = false;
    volatile uint32_t s_reconnectCount = 0;
    TaskHandle_t s_monitorTaskHandle = nullptr;

    void applyRadioSettings() {
        // Disable Wi-Fi power-saving: critical for low, consistent latency.
        // Modem sleep introduces tens-of-milliseconds jitter per packet,
        // which is unacceptable for a real-time video stream.
        WiFi.setSleep(false);
        WiFi.setTxPower(WIFI_POWER_19_5dBm);
    }

    bool connectBlocking(uint32_t timeoutMs) {
        Serial.printf("[WiFi] Connecting to SSID '%s'...\n", WIFI_SSID);

        WiFi.mode(WIFI_STA);
        WiFi.disconnect(true, true);
        delay(100);

#if WIFI_USE_STATIC_IP
        WiFi.config(WIFI_STATIC_IP, WIFI_STATIC_GATEWAY, WIFI_STATIC_SUBNET, WIFI_STATIC_DNS);
#endif

        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

        uint32_t start = millis();
        while (WiFi.status() != WL_CONNECTED) {
            if (millis() - start > timeoutMs) {
                Serial.println("[WiFi] Connect timed out");
                return false;
            }
            delay(200);
        }

        applyRadioSettings();

        Serial.printf("[WiFi] Connected. IP=%s RSSI=%d dBm\n",
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
    }

    void monitorTask(void* pvParameters) {
        uint32_t retryDelay = WIFI_RECONNECT_RETRY_DELAY_MS;

        for (;;) {
            wl_status_t status = WiFi.status();

            if (status == WL_CONNECTED) {
                s_connected = true;
                retryDelay = WIFI_RECONNECT_RETRY_DELAY_MS; // reset backoff
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }

            // Link is down.
            if (s_connected) {
                Serial.println("[WiFi] Link lost, will attempt reconnect");
            }
            s_connected = false;

            Serial.printf("[WiFi] Reconnect attempt (backoff=%u ms)...\n", retryDelay);
            bool ok = connectBlocking(WIFI_CONNECT_TIMEOUT_MS);
            if (ok) {
                s_connected = true;
                s_reconnectCount++;
                retryDelay = WIFI_RECONNECT_RETRY_DELAY_MS;
            } else {
                vTaskDelay(pdMS_TO_TICKS(retryDelay));
                retryDelay = min(retryDelay * 2, (uint32_t)WIFI_MAX_RETRY_DELAY_MS);
            }
        }
    }
}

namespace WifiManager {

bool begin() {
    bool ok = connectBlocking(WIFI_CONNECT_TIMEOUT_MS);
    s_connected = ok;

    BaseType_t created = xTaskCreatePinnedToCore(
        monitorTask,
        "wifi_monitor",
        WIFI_MGR_TASK_STACK_SIZE,
        nullptr,
        WIFI_MGR_TASK_PRIORITY,
        &s_monitorTaskHandle,
        WIFI_MGR_TASK_CORE
    );

    if (created != pdPASS) {
        Serial.println("[WiFi] FATAL: failed to create monitor task");
    }

    return ok;
}

bool isConnected() {
    return s_connected;
}

String localIP() {
    if (!s_connected) {
        return String("0.0.0.0");
    }
    return WiFi.localIP().toString();
}

uint32_t reconnectCount() {
    return s_reconnectCount;
}

} // namespace WifiManager
