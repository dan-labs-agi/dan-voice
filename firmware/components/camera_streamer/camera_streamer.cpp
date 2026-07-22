/**
 * camera_streamer.cpp
 *
 * Entry point for this component. Native-ESP-IDF equivalent of what
 * ESP32_HighSpeed_Camera.ino's setup()/loop() did standalone: bring up the
 * camera, bring up Wi-Fi, start the capture/network/monitor pipeline.
 */

#include "camera_streamer.h"

#include "esp_log.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "config.h"
#include "camera.h"
#include "wifi_manager.h"
#include "streamer.h"

namespace {

const char* TAG = "camera_streamer";

void blinkError(int times) {
    gpio_reset_pin((gpio_num_t)LED_BUILTIN_PIN);
    gpio_set_direction((gpio_num_t)LED_BUILTIN_PIN, GPIO_MODE_OUTPUT);
    for (int i = 0; i < times; i++) {
        gpio_set_level((gpio_num_t)LED_BUILTIN_PIN, 0); // active-low LED on
        vTaskDelay(pdMS_TO_TICKS(150));
        gpio_set_level((gpio_num_t)LED_BUILTIN_PIN, 1); // off
        vTaskDelay(pdMS_TO_TICKS(150));
    }
}

void setupTask(void* /*pvParameters*/) {
    ESP_LOGI(TAG, "=================================================");
    ESP_LOGI(TAG, " ESP32-S3 XIAO Sense — High-Speed Camera Streamer");
    ESP_LOGI(TAG, "=================================================");

    // Camera init, with a few retries since sensor bring-up can be flaky
    // immediately after power-on.
    CameraStatus camStatus = CameraStatus::ERR_INIT_FAILED;
    for (int attempt = 1; attempt <= 3 && camStatus != CameraStatus::OK; attempt++) {
        ESP_LOGI(TAG, "Camera init attempt %d/3...", attempt);
        camStatus = CameraModule::init();
        if (camStatus != CameraStatus::OK) {
            ESP_LOGW(TAG, "Camera init failed: %s", CameraModule::statusToString(camStatus));
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }

    if (camStatus != CameraStatus::OK) {
        ESP_LOGE(TAG, "FATAL: camera failed to initialize after 3 attempts.");
        ESP_LOGE(TAG, "Halting camera/streaming pipeline (BLE provisioning keeps running independently).");
        for (;;) {
            blinkError(2);
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }

    // Wi-Fi init. begin() blocks for the initial connect but then hands off
    // reconnect duty to its own background task, so this call returning
    // false (e.g. AP temporarily unreachable) is not fatal — the monitor
    // task will keep retrying and the network task will simply wait.
    bool wifiOk = WifiManager::begin();
    if (!wifiOk) {
        ESP_LOGW(TAG, "Initial Wi-Fi connect failed; background retry is active.");
    } else {
        ESP_LOGI(TAG, "Wi-Fi connected. IP=%s", WifiManager::localIP().c_str());
        ESP_LOGI(TAG, "Connect with: nc/python client -> %s:%d", WifiManager::localIP().c_str(), TCP_SERVER_PORT);
    }

    // Start the capture/network/monitor pipeline.
    Streamer::begin();

    ESP_LOGI(TAG, "Initialization complete. Streaming pipeline is live.");
    vTaskDelete(nullptr);
}

} // namespace

extern "C" void camera_streamer_start(void) {
    xTaskCreatePinnedToCore(
        setupTask, "camera_streamer_setup", 8192,
        nullptr, tskIDLE_PRIORITY + 1, nullptr, WIFI_MGR_TASK_CORE
    );
}
