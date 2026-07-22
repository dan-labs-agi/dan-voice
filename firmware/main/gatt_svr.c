#include <assert.h>
#include <string.h>

#include "esp_log.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include "gatt_svr.h"

static const char *TAG = "gatt_svr";

/* Dani Voice provisioning service. UUIDs are project-specific, randomly
 * generated — not borrowed from any spec or example. BLE_UUID128_INIT
 * takes bytes in over-the-air (little-endian) order, i.e. the standard
 * UUID string's bytes reversed. */
const ble_uuid128_t gatt_svr_svc_uuid =
    BLE_UUID128_INIT(0xd6, 0x0b, 0x1a, 0xcf, 0x37, 0x10, 0x3e, 0x96,
                      0x41, 0x45, 0x38, 0x92, 0x2f, 0x69, 0x1e, 0xfd);

static const ble_uuid128_t token_chr_uuid =
    BLE_UUID128_INIT(0x62, 0x36, 0x36, 0x64, 0x55, 0xa7, 0x14, 0xa2,
                      0xf2, 0x48, 0x3b, 0x29, 0x19, 0x40, 0xff, 0x34);

static const ble_uuid128_t status_chr_uuid =
    BLE_UUID128_INIT(0xe5, 0x25, 0x0f, 0x3e, 0x21, 0xf2, 0x8f, 0x8f,
                      0x85, 0x4c, 0x81, 0x61, 0xdf, 0x93, 0xc9, 0x1d);

/* Device-scoped token. Phase 6 only accepts and stores the bytes a phone
 * writes here — no parsing, validation, or use of the token yet (that's
 * Phase 7+). Plain RAM only, per the architecture doc: never written to
 * NVS/flash, so a reboot clears it and forces re-provisioning. */
static uint8_t device_token_buf[TOKEN_CHR_MAX_LEN];
static uint16_t device_token_len;

static uint8_t device_status = DEVICE_STATUS_IDLE;
static uint16_t status_chr_val_handle;

static int
token_chr_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                     struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    /* Minimal shape validation only (non-empty, within the buffer). Actual
     * token validation against the backend is Phase 8's job. */
    uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
    if (om_len == 0 || om_len > TOKEN_CHR_MAX_LEN) {
        gatt_svr_set_status(DEVICE_STATUS_FAILED);
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }

    gatt_svr_set_status(DEVICE_STATUS_PROVISIONING);

    uint16_t copied_len = 0;
    int rc = ble_hs_mbuf_to_flat(ctxt->om, device_token_buf, TOKEN_CHR_MAX_LEN, &copied_len);
    if (rc != 0) {
        gatt_svr_set_status(DEVICE_STATUS_FAILED);
        return BLE_ATT_ERR_UNLIKELY;
    }
    device_token_len = copied_len;

    ESP_LOGI(TAG, "token characteristic write: %u bytes (conn_handle=%d)",
             copied_len, conn_handle);
    gatt_svr_set_status(DEVICE_STATUS_OK);
    return 0;
}

static int
status_chr_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                      struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    int rc = os_mbuf_append(ctxt->om, &device_status, sizeof(device_status));
    return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

static const struct ble_gatt_svc_def gatt_svr_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &gatt_svr_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                .uuid = &token_chr_uuid.u,
                .access_cb = token_chr_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC,
            },
            {
                .uuid = &status_chr_uuid.u,
                .access_cb = status_chr_access_cb,
                .val_handle = &status_chr_val_handle,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_READ_ENC |
                         BLE_GATT_CHR_F_NOTIFY,
            },
            {
                0, /* No more characteristics in this service. */
            },
        },
    },
    {
        0, /* No more services. */
    },
};

void
gatt_svr_set_status(device_status_t status)
{
    device_status = (uint8_t)status;
    if (status_chr_val_handle != 0) {
        ble_gatts_chr_updated(status_chr_val_handle);
    }
}

void
gatt_svr_register_cb(struct ble_gatt_register_ctxt *ctxt, void *arg)
{
    char buf[BLE_UUID_STR_LEN];

    switch (ctxt->op) {
    case BLE_GATT_REGISTER_OP_SVC:
        ESP_LOGD(TAG, "registered service %s with handle=%d",
                 ble_uuid_to_str(ctxt->svc.svc_def->uuid, buf), ctxt->svc.handle);
        break;

    case BLE_GATT_REGISTER_OP_CHR:
        ESP_LOGD(TAG, "registered characteristic %s with def_handle=%d val_handle=%d",
                 ble_uuid_to_str(ctxt->chr.chr_def->uuid, buf),
                 ctxt->chr.def_handle, ctxt->chr.val_handle);
        break;

    case BLE_GATT_REGISTER_OP_DSC:
        ESP_LOGD(TAG, "registered descriptor %s with handle=%d",
                 ble_uuid_to_str(ctxt->dsc.dsc_def->uuid, buf), ctxt->dsc.handle);
        break;

    default:
        assert(0);
        break;
    }
}

int
gatt_svr_init(void)
{
    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(gatt_svr_svcs);
    if (rc != 0) {
        return rc;
    }

    rc = ble_gatts_add_svcs(gatt_svr_svcs);
    if (rc != 0) {
        return rc;
    }

    return 0;
}
