#include <assert.h>
#include <string.h>

#include "esp_log.h"
#include "nvs_flash.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include "gatt_svr.h"
#include "camera_streamer.h"

static const char *TAG = "dani_voice_ble";

static uint8_t own_addr_type;

/* Provided by the NimBLE "store/config" component; registers the RAM-only
 * bonding store (CONFIG_BT_NIMBLE_NVS_PERSIST is left at its default n). */
void ble_store_config_init(void);

static int ble_gap_event_handler(struct ble_gap_event *event, void *arg);

static void
start_advertising(void)
{
    struct ble_hs_adv_fields fields;
    memset(&fields, 0, sizeof fields);

    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;

    const char *name = ble_svc_gap_device_name();
    fields.name = (uint8_t *)name;
    fields.name_len = strlen(name);
    fields.name_is_complete = 1;

    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "error setting advertisement data; rc=%d", rc);
        return;
    }

    /* The 128-bit service UUID doesn't fit alongside flags + full device
     * name in the 31-byte primary advertising packet, so it goes in the
     * scan response instead. Legacy undirected-connectable advertising
     * (BLE_GAP_CONN_MODE_UND) is always scannable (ADV_IND), so a
     * scan-response payload is always fetchable. Without this, Web
     * Bluetooth's requestDevice({filters: [{services: [...]}]}) never
     * discovers this device — it only matches on advertised/scan-response
     * UUIDs, not the device name. */
    struct ble_hs_adv_fields rsp_fields;
    memset(&rsp_fields, 0, sizeof rsp_fields);
    rsp_fields.uuids128 = &gatt_svr_svc_uuid;
    rsp_fields.num_uuids128 = 1;
    rsp_fields.uuids128_is_complete = 1;

    rc = ble_gap_adv_rsp_set_fields(&rsp_fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "error setting scan response data; rc=%d", rc);
        return;
    }

    struct ble_gap_adv_params adv_params;
    memset(&adv_params, 0, sizeof adv_params);
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    rc = ble_gap_adv_start(own_addr_type, NULL, BLE_HS_FOREVER, &adv_params,
                            ble_gap_event_handler, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "error enabling advertisement; rc=%d", rc);
        return;
    }

    ESP_LOGI(TAG, "advertising started");
}

static int
ble_gap_event_handler(struct ble_gap_event *event, void *arg)
{
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        ESP_LOGI(TAG, "connection %s; status=%d",
                 event->connect.status == 0 ? "established" : "failed",
                 event->connect.status);
        if (event->connect.status != 0) {
            /* Connection attempt failed; resume advertising. */
            start_advertising();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "disconnect; reason=%d", event->disconnect.reason);
        gatt_svr_set_status(DEVICE_STATUS_IDLE);
        start_advertising();
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        start_advertising();
        return 0;

    case BLE_GAP_EVENT_ENC_CHANGE: {
        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(event->enc_change.conn_handle, &desc) == 0) {
            ESP_LOGI(TAG, "encryption change; status=%d encrypted=%d bonded=%d",
                     event->enc_change.status, desc.sec_state.encrypted,
                     desc.sec_state.bonded);
        }
        return 0;
    }

    case BLE_GAP_EVENT_SUBSCRIBE:
        ESP_LOGI(TAG, "subscribe event; attr_handle=%d reason=%d prev_notify=%d cur_notify=%d",
                 event->subscribe.attr_handle, event->subscribe.reason,
                 event->subscribe.prev_notify, event->subscribe.cur_notify);
        return 0;

    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "mtu update; conn_handle=%d mtu=%d",
                 event->mtu.conn_handle, event->mtu.value);
        return 0;

    case BLE_GAP_EVENT_REPEAT_PAIRING: {
        /* We already have a bond with this peer, but it's attempting a
         * fresh pairing (e.g. it lost its side of the bond). Drop our old
         * bond and let the pairing retry rather than rejecting it. */
        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc) == 0) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    }

    default:
        return 0;
    }
}

static void
on_reset(int reason)
{
    ESP_LOGE(TAG, "resetting state; reason=%d", reason);
}

static void
on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    assert(rc == 0);

    rc = ble_hs_id_infer_auto(0, &own_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "error determining address type; rc=%d", rc);
        return;
    }

    start_advertising();
}

static void
host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void
app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(nimble_port_init());

    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.gatts_register_cb = gatt_svr_register_cb;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;

    /* LE Secure Connections, Just Works, with bonding. This board has no
     * display or keyboard, so the MITM-protected association methods
     * (Numeric Comparison / Passkey Entry) aren't available — those
     * require I/O capability on at least one side of the pairing. SC still
     * gets us ECDH-based key exchange and an encrypted, bonded link. */
    ble_hs_cfg.sm_io_cap = BLE_HS_IO_NO_INPUT_OUTPUT;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 0;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;

    int rc = gatt_svr_init();
    assert(rc == 0);

    rc = ble_svc_gap_device_name_set("dani-voice");
    assert(rc == 0);

    /* Registers the RAM-only bonding store (see ble_store_config_init
     * above) — bonding keys never touch NVS/flash. */
    ble_store_config_init();

    nimble_port_freertos_init(host_task);

    /* TEMPORARY DIAGNOSTIC STATE — DO NOT SHIP LIKE THIS.
     * camera_streamer_start() deliberately disabled to isolate BLE
     * provisioning debugging from Wi-Fi/BLE coexistence entirely: with
     * WIFI_PS_NONE, the camera's Wi-Fi STA activity was starving BLE of
     * radio time and killing every connection ~360ms after it formed
     * (BLE_ERR_UNK_CONN_ID during LE_Start_Encryption). Switching to
     * WIFI_PS_MIN_MODEM (see wifi_manager.cpp's applyRadioSettings) was
     * meant to fix that, but a token-write failure persisted afterward, so
     * camera/Wi-Fi is fully disabled here to confirm whether BLE
     * write/pairing is solid with zero coexistence pressure before
     * re-introducing it. This call MUST be restored before this is
     * considered done — see CLAUDE.md's "Out-of-scope addition" section
     * (camera+BLE coexistence is a permanent architectural requirement,
     * not something to drop) and PROGRESS.md's diagnostic entry. */
    // camera_streamer_start();
}
