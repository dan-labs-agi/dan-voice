/**
 * camera.cpp
 *
 * Implementation of the camera hardware abstraction layer.
 */

#include "camera.h"
#include "config.h"

#include <cstring>

#include "esp_log.h"
#include "esp_heap_caps.h"

namespace {
    bool s_initialized = false;
    const char* TAG = "camera";

    bool psramAvailable() {
        return heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0;
    }
}

namespace CameraModule {

CameraStatus init() {
    if (s_initialized) {
        return CameraStatus::OK;
    }

    camera_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));

    cfg.ledc_channel = LEDC_CHANNEL_0;
    cfg.ledc_timer   = LEDC_TIMER_0;

    cfg.pin_d0    = CAM_PIN_D0;
    cfg.pin_d1    = CAM_PIN_D1;
    cfg.pin_d2    = CAM_PIN_D2;
    cfg.pin_d3    = CAM_PIN_D3;
    cfg.pin_d4    = CAM_PIN_D4;
    cfg.pin_d5    = CAM_PIN_D5;
    cfg.pin_d6    = CAM_PIN_D6;
    cfg.pin_d7    = CAM_PIN_D7;
    cfg.pin_xclk  = CAM_PIN_XCLK;
    cfg.pin_pclk  = CAM_PIN_PCLK;
    cfg.pin_vsync = CAM_PIN_VSYNC;
    cfg.pin_href  = CAM_PIN_HREF;
    cfg.pin_sccb_sda = CAM_PIN_SIOD;
    cfg.pin_sccb_scl = CAM_PIN_SIOC;
    cfg.pin_pwdn  = CAM_PIN_PWDN;
    cfg.pin_reset = CAM_PIN_RESET;

    cfg.xclk_freq_hz = CAM_XCLK_FREQ_HZ;
    cfg.pixel_format = CAM_PIXEL_FORMAT;

    // Frame buffer strategy: allocate in PSRAM, use multiple buffers, and
    // always hand the caller the freshest frame rather than queueing stale
    // ones inside the driver itself (we do our own queueing at the
    // application level with FRAME_QUEUE_DEPTH).
    if (psramAvailable()) {
        cfg.frame_size   = CAM_DEFAULT_FRAMESIZE;
        cfg.jpeg_quality = CAM_DEFAULT_JPEG_QUALITY;
        cfg.fb_count     = CAM_FB_COUNT;
        cfg.fb_location  = CAM_FB_LOCATION;
        cfg.grab_mode    = CAM_GRAB_MODE;
    } else {
        // Fallback if PSRAM is somehow unavailable: smaller frame, single
        // buffer, DRAM. Performance targets will not be met in this mode,
        // but the firmware still boots and streams instead of crashing.
        ESP_LOGW(TAG, "PSRAM not found, falling back to reduced settings");
        cfg.frame_size   = FRAMESIZE_QQVGA;
        cfg.jpeg_quality = 15;
        cfg.fb_count     = 1;
        cfg.fb_location  = CAMERA_FB_IN_DRAM;
        cfg.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
    }

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init failed with error 0x%x", err);
        return CameraStatus::ERR_INIT_FAILED;
    }

    sensor_t* sensor = esp_camera_sensor_get();
    if (sensor == nullptr) {
        ESP_LOGE(TAG, "Failed to get sensor handle after init");
        return CameraStatus::ERR_SENSOR_NOT_FOUND;
    }

    // OV3660-specific tuning. These calls are safe no-ops on sensors that
    // don't support a given control; the esp32-camera driver checks
    // capability internally.
    sensor->set_vflip(sensor, 1);
    sensor->set_hmirror(sensor, 1);
    sensor->set_brightness(sensor, 0);
    sensor->set_saturation(sensor, 0);
    sensor->set_gain_ctrl(sensor, 1);
    sensor->set_exposure_ctrl(sensor, 1);
    sensor->set_whitebal(sensor, 1);
    sensor->set_awb_gain(sensor, 1);

    // Re-apply framesize/quality explicitly in case the sensor init
    // reset something. Also lets us log the state we ended up in.
    sensor->set_framesize(sensor, cfg.frame_size);
    sensor->set_quality(sensor, cfg.jpeg_quality);

    s_initialized = true;
    ESP_LOGI(TAG, "Initialized successfully");
    ESP_LOGI(TAG, "Frame size=%d, quality=%d, fb_count=%d",
             (int)cfg.frame_size, cfg.jpeg_quality, cfg.fb_count);

    return CameraStatus::OK;
}

camera_fb_t* capture() {
    if (!s_initialized) {
        return nullptr;
    }
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb == nullptr) {
        ESP_LOGW(TAG, "esp_camera_fb_get() returned null (capture failed)");
    }
    return fb;
}

void release(camera_fb_t* fb) {
    if (fb != nullptr) {
        esp_camera_fb_return(fb);
    }
}

CameraStatus setFrameSize(framesize_t size) {
    if (!s_initialized) {
        return CameraStatus::ERR_INIT_FAILED;
    }
    sensor_t* sensor = esp_camera_sensor_get();
    if (sensor == nullptr) {
        return CameraStatus::ERR_SENSOR_NOT_FOUND;
    }
    if (sensor->set_framesize(sensor, size) != 0) {
        return CameraStatus::ERR_INVALID_PARAM;
    }
    ESP_LOGI(TAG, "Frame size changed to %d", (int)size);
    return CameraStatus::OK;
}

CameraStatus setJpegQuality(int quality) {
    if (!s_initialized) {
        return CameraStatus::ERR_INIT_FAILED;
    }
    if (quality < 0 || quality > 63) {
        return CameraStatus::ERR_INVALID_PARAM;
    }
    sensor_t* sensor = esp_camera_sensor_get();
    if (sensor == nullptr) {
        return CameraStatus::ERR_SENSOR_NOT_FOUND;
    }
    sensor->set_quality(sensor, quality);
    ESP_LOGI(TAG, "JPEG quality changed to %d", quality);
    return CameraStatus::OK;
}

bool isInitialized() {
    return s_initialized;
}

const char* statusToString(CameraStatus status) {
    switch (status) {
        case CameraStatus::OK:                  return "OK";
        case CameraStatus::ERR_INIT_FAILED:      return "ERR_INIT_FAILED";
        case CameraStatus::ERR_SENSOR_NOT_FOUND: return "ERR_SENSOR_NOT_FOUND";
        case CameraStatus::ERR_CAPTURE_FAILED:   return "ERR_CAPTURE_FAILED";
        case CameraStatus::ERR_INVALID_PARAM:    return "ERR_INVALID_PARAM";
        default:                                 return "UNKNOWN";
    }
}

} // namespace CameraModule
