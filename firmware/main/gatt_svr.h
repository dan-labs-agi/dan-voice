#pragma once

#include "host/ble_gatt.h"
#include "host/ble_uuid.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Generous enough for a JWT device token; long-write via NimBLE's queued
 * writes handles anything larger than the negotiated ATT MTU. */
#define TOKEN_CHR_MAX_LEN 600

typedef enum {
    DEVICE_STATUS_IDLE = 0,
    DEVICE_STATUS_PROVISIONING = 1,
    DEVICE_STATUS_OK = 2,
    DEVICE_STATUS_FAILED = 3,
} device_status_t;

/* The provisioning service's UUID — exposed so main.c can advertise it in
 * the scan response (Web Bluetooth's requestDevice({filters: [{services}]})
 * only matches devices whose advertising/scan-response data carries the
 * UUID; it won't discover a device by name-only advertising). */
extern const ble_uuid128_t gatt_svr_svc_uuid;

void gatt_svr_register_cb(struct ble_gatt_register_ctxt *ctxt, void *arg);
int gatt_svr_init(void);
void gatt_svr_set_status(device_status_t status);

#ifdef __cplusplus
}
#endif
