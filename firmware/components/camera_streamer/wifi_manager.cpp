/**
 * wifi_manager.cpp
 *
 * Native ESP-IDF implementation of Wi-Fi lifecycle management.
 *
 * Arduino's WiFi.h hides ESP-IDF's event-driven connection model behind a
 * synchronous WiFi.status() poll. ESP-IDF itself has no such call — a
 * connect attempt's outcome only arrives via WIFI_EVENT_STA_DISCONNECTED
 * or IP_EVENT_STA_GOT_IP callbacks. So "blocking connect with timeout" is
 * built here as: fire esp_wifi_connect(), then block on an EventGroup bit
 * that the event handler sets/clears. The reconnect monitor task is driven
 * the same way — it blocks on the "disconnected" bit rather than polling.
 */

#include "wifi_manager.h"
#include "config.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

namespace {

const char* TAG = "wifi_manager";

constexpr EventBits_t WIFI_CONNECTED_BIT = BIT0;
constexpr EventBits_t WIFI_DISCONNECTED_BIT = BIT1;

EventGroupHandle_t s_wifiEventGroup = nullptr;
esp_netif_t* s_staNetif = nullptr;
volatile bool s_connected = false;
volatile uint32_t s_reconnectCount = 0;
esp_ip4_addr_t s_ip = {};
TaskHandle_t s_monitorTaskHandle = nullptr;

void applyRadioSettings() {
    // WIFI_PS_NONE (no modem sleep) starves BLE of radio time under
    // software coexistence: Wi-Fi never yields idle slices, so the
    // coexistence arbiter can't schedule BLE connection events, and every
    // BLE link dies (supervision timeout) within ~1 connection interval of
    // forming. WIFI_PS_MIN_MODEM is ESP-IDF's standard recommendation for
    // BT/Wi-Fi coexistence — Wi-Fi still wakes for every DTIM beacon, but
    // yields between beacons so BLE gets scheduled reliably.
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    // 78 quarter-dBm units == 19.5 dBm (Arduino's WIFI_POWER_19_5dBm enum,
    // which ESP-IDF has no equivalent named constant for).
    esp_wifi_set_max_tx_power(78);
}

void wifiEventHandler(void* /*arg*/, esp_event_base_t eventBase, int32_t eventId, void* eventData) {
    if (eventBase == WIFI_EVENT && eventId == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (eventBase == WIFI_EVENT && eventId == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        if (s_wifiEventGroup != nullptr) {
            xEventGroupClearBits(s_wifiEventGroup, WIFI_CONNECTED_BIT);
            xEventGroupSetBits(s_wifiEventGroup, WIFI_DISCONNECTED_BIT);
        }
    } else if (eventBase == IP_EVENT && eventId == IP_EVENT_STA_GOT_IP) {
        auto* event = static_cast<ip_event_got_ip_t*>(eventData);
        s_ip = event->ip_info.ip;
        s_connected = true;

        wifi_ap_record_t apInfo = {};
        int8_t rssi = 0;
        if (esp_wifi_sta_get_ap_info(&apInfo) == ESP_OK) {
            rssi = apInfo.rssi;
        }
        ESP_LOGI(TAG, "Connected. IP=" IPSTR " RSSI=%d dBm", IP2STR(&event->ip_info.ip), (int)rssi);
        if (s_wifiEventGroup != nullptr) {
            xEventGroupClearBits(s_wifiEventGroup, WIFI_DISCONNECTED_BIT);
            xEventGroupSetBits(s_wifiEventGroup, WIFI_CONNECTED_BIT);
        }
    }
}

// Blocks (up to timeoutMs) waiting for the link to come back up after a
// disconnect. Returns true once IP_EVENT_STA_GOT_IP fires.
bool reconnectBlocking(uint32_t timeoutMs) {
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK && err != ESP_ERR_WIFI_CONN) {
        ESP_LOGW(TAG, "esp_wifi_connect() failed, err=0x%x", err);
        return false;
    }
    EventBits_t bits = xEventGroupWaitBits(
        s_wifiEventGroup, WIFI_CONNECTED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeoutMs));
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

void monitorTask(void* /*pvParameters*/) {
    uint32_t retryDelay = WIFI_RECONNECT_RETRY_DELAY_MS;

    for (;;) {
        // Block until the event handler reports a disconnect; this task
        // does no polling of its own.
        EventBits_t bits = xEventGroupWaitBits(
            s_wifiEventGroup, WIFI_DISCONNECTED_BIT, pdTRUE, pdFALSE, portMAX_DELAY);
        if (!(bits & WIFI_DISCONNECTED_BIT)) {
            continue;
        }

        ESP_LOGW(TAG, "Link lost, will attempt reconnect (backoff=%u ms)", (unsigned)retryDelay);
        vTaskDelay(pdMS_TO_TICKS(retryDelay));

        bool ok = reconnectBlocking(WIFI_CONNECT_TIMEOUT_MS);
        if (ok) {
            applyRadioSettings();
            s_reconnectCount++;
            retryDelay = WIFI_RECONNECT_RETRY_DELAY_MS;
        } else {
            retryDelay = std::min(retryDelay * 2, (uint32_t)WIFI_MAX_RETRY_DELAY_MS);
        }
    }
}

} // namespace

namespace WifiManager {

bool begin() {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_staNetif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t initCfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&initCfg));

    s_wifiEventGroup = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifiEventHandler, nullptr, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifiEventHandler, nullptr, nullptr));

    wifi_config_t wifiConfig = {};
    std::snprintf(reinterpret_cast<char*>(wifiConfig.sta.ssid), sizeof(wifiConfig.sta.ssid), "%s", WIFI_SSID);
    std::snprintf(reinterpret_cast<char*>(wifiConfig.sta.password), sizeof(wifiConfig.sta.password), "%s", WIFI_PASSWORD);
    wifiConfig.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifiConfig));

#if WIFI_USE_STATIC_IP
    ESP_ERROR_CHECK(esp_netif_dhcpc_stop(s_staNetif));

    esp_netif_ip_info_t ipInfo = {};
    esp_netif_str_to_ip4(WIFI_STATIC_IP_ADDR, &ipInfo.ip);
    esp_netif_str_to_ip4(WIFI_STATIC_GATEWAY_ADDR, &ipInfo.gw);
    esp_netif_str_to_ip4(WIFI_STATIC_SUBNET_ADDR, &ipInfo.netmask);
    ESP_ERROR_CHECK(esp_netif_set_ip_info(s_staNetif, &ipInfo));

    esp_netif_dns_info_t dnsInfo = {};
    dnsInfo.ip.type = ESP_IPADDR_TYPE_V4;
    esp_netif_str_to_ip4(WIFI_STATIC_DNS_ADDR, &dnsInfo.ip.u_addr.ip4);
    ESP_ERROR_CHECK(esp_netif_set_dns_info(s_staNetif, ESP_NETIF_DNS_MAIN, &dnsInfo));
#endif

    ESP_LOGI(TAG, "Connecting to SSID '%s'...", WIFI_SSID);
    ESP_ERROR_CHECK(esp_wifi_start());
    applyRadioSettings();

    EventBits_t bits = xEventGroupWaitBits(
        s_wifiEventGroup, WIFI_CONNECTED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
    bool ok = (bits & WIFI_CONNECTED_BIT) != 0;
    s_connected = ok;
    if (!ok) {
        ESP_LOGW(TAG, "Connect timed out");
    }

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
        ESP_LOGE(TAG, "FATAL: failed to create monitor task");
    }

    return ok;
}

bool isConnected() {
    return s_connected;
}

std::string localIP() {
    if (!s_connected) {
        return std::string("0.0.0.0");
    }
    char buf[16];
    std::snprintf(buf, sizeof(buf), IPSTR, IP2STR(&s_ip));
    return std::string(buf);
}

uint32_t reconnectCount() {
    return s_reconnectCount;
}

} // namespace WifiManager
