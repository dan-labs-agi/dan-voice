// UUIDs mirror firmware/main/gatt_svr.c's "Dani Voice provisioning" service
// (its BLE_UUID128_INIT byte arrays, reversed back to standard UUID string
// order — see that file's comment on over-the-air vs. standard byte order).
export const SERVICE_UUID = "fd1e692f-9238-4541-963e-1037cf1a0bd6";
export const TOKEN_CHARACTERISTIC_UUID = "34ff4019-293b-48f2-a214-a75564363662";
export const STATUS_CHARACTERISTIC_UUID = "1dc993df-6181-4c85-8f8f-f2213e0f25e5";

// Mirrors device_status_t in firmware/main/gatt_svr.h.
export type DeviceStatus = "idle" | "provisioning" | "ok" | "failed";

const STATUS_BY_CODE: Record<number, DeviceStatus> = {
  0: "idle",
  1: "provisioning",
  2: "ok",
  3: "failed",
};

export class BleProvisioningError extends Error {}

function describeError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class BleTimeoutError extends Error {}

// Some Android/Chrome Web Bluetooth GATT operations can silently never
// resolve or reject if the underlying callback from the OS Bluetooth stack
// is lost — nothing in the spec or the implementation times these out on
// its own, so a hung call freezes the UI forever with no error. Every GATT
// step below is wrapped in this so a hang becomes an actionable error
// instead.
//
// TEMPORARY DIAGNOSTIC LOGGING — every step logs to the console on entry,
// success, and failure so a failing provisioning attempt can be pinned to
// an exact stage from the browser devtools console, instead of only seeing
// the final wrapped BleProvisioningError message.
async function withTimeout<T>(step: string, ms: number, op: () => Promise<T>): Promise<T> {
  console.log(`[ble] -> ${step}`);
  try {
    const result = await Promise.race([
      op(),
      sleep(ms).then(() => {
        throw new BleTimeoutError(`Timed out waiting for: ${step}`);
      }),
    ]);
    console.log(`[ble] OK ${step}`);
    return result;
  } catch (err) {
    console.error(`[ble] FAIL ${step}:`, err);
    throw err;
  }
}

// Android's Bluetooth stack commonly triggers the actual encryption/bonding
// negotiation lazily, on the *first* GATT operation against an encrypted
// characteristic, rather than eagerly when gatt.connect() resolves. LE
// Secure Connections' ECDH exchange can take a couple of seconds, so that
// first operation often fails with a generic "GATT Error Unknown" while
// bonding finishes in the background — a retry after a short delay
// typically succeeds once encryption has actually settled.
//
// Deliberately retries on a genuine (fast) rejection only, never wraps the
// individual attempt in withTimeout(): Android's BluetoothGatt allows only
// one outstanding GATT operation per connection, and Promise.race doesn't
// cancel the losing side — a "timed out" attempt is often still alive in
// the background, so retrying it collides with itself
// ("GATT operation already in progress"). The caller applies one outer
// timeout around the whole retry sequence instead, purely to stop an
// unresponsive attempt from freezing the UI forever — it does not retry
// again itself if that outer timeout fires.
//
// A write that's larger than the negotiated ATT MTU (the device token
// always is) becomes a NimBLE "long write": a sequence of Prepare Write
// Request chunks queued server-side, committed by one Execute Write. If the
// first attempt fails partway through that sequence, some chunks are left
// sitting in NimBLE's per-connection prepare-write queue
// (ble_att_svr's basc_prep_list) — Web Bluetooth has no primitive to send an
// explicit Cancel Execute Write, and that queue is only ever cleared by a
// disconnect. Simply calling the write again on the same connection queues
// a second offset-0 chunk on top of the stale one, which NimBLE's
// ble_att_svr_prep_validate() rejects with BLE_ATT_ERR_INVALID_OFFSET
// (rc=7) — permanently, for any further write on that connection. So the
// retry must reconnect first, to force NimBLE to drop the stale queue along
// with the old connection state.
async function withEncryptionRetry<T>(
  op: () => Promise<T>,
  reconnect: () => Promise<void>,
): Promise<T> {
  console.log("[ble] -> write attempt 1");
  try {
    const result = await op();
    console.log("[ble] OK write attempt 1");
    return result;
  } catch (err) {
    console.error("[ble] FAIL write attempt 1:", err);
    await sleep(2000);
    console.log("[ble] -> reconnect before retry");
    try {
      await reconnect();
      console.log("[ble] OK reconnect before retry");
    } catch (reconnectErr) {
      console.error("[ble] FAIL reconnect before retry:", reconnectErr);
      throw reconnectErr;
    }
    console.log("[ble] -> write attempt 2");
    try {
      const result = await op();
      console.log("[ble] OK write attempt 2");
      return result;
    } catch (retryErr) {
      console.error("[ble] FAIL write attempt 2:", retryErr);
      throw retryErr;
    }
  }
}

export function isWebBluetoothAvailable(): boolean {
  return typeof navigator !== "undefined" && navigator.bluetooth !== undefined;
}

async function connectAndDiscover(
  device: BluetoothDevice,
  onStatus: (status: DeviceStatus) => void,
): Promise<{ service: BluetoothRemoteGATTService; tokenChar: BluetoothRemoteGATTCharacteristic }> {
  const connected = await withTimeout("connect", 15_000, async () => await device.gatt?.connect());
  if (!connected) throw new Error("no gatt server");

  const service = await withTimeout("find service", 10_000, () =>
    connected.getPrimaryService(SERVICE_UUID),
  );

  const statusChar = await withTimeout("find status characteristic", 10_000, () =>
    service.getCharacteristic(STATUS_CHARACTERISTIC_UUID),
  );
  statusChar.addEventListener("characteristicvaluechanged", () => {
    const value = statusChar.value;
    if (!value) return;
    onStatus(STATUS_BY_CODE[value.getUint8(0)] ?? "idle");
  });
  await withTimeout("subscribe to status", 10_000, () => statusChar.startNotifications());

  const tokenChar = await withTimeout("find token characteristic", 10_000, () =>
    service.getCharacteristic(TOKEN_CHARACTERISTIC_UUID),
  );

  return { service, tokenChar };
}

// Android's BluetoothGatt can spuriously report "GATT Server is
// disconnected" on the very first GATT operation attempted after
// gatt.connect() resolves — connect() resolving only means the link
// exists, not that Android's internal bonding/encryption negotiation has
// settled yet. Unlike the token write's own version of this race (see
// withEncryptionRetry below), this isn't limited to operations gated on
// encryption — even a plain, unencrypted service-discovery call can be
// spuriously rejected while that negotiation is still in-flight, then
// succeed fine a moment later once it's done. Recovering requires an
// actual reconnect (drop the link and re-establish it), not just retrying
// the same call against the same, already-reported-disconnected server
// object — confirmed by the browser's own error message pointing at
// exactly that fix: "(Re)connect first with `device.gatt.connect`."
async function connectAndDiscoverWithRetry(
  device: BluetoothDevice,
  onStatus: (status: DeviceStatus) => void,
): Promise<{ service: BluetoothRemoteGATTService; tokenChar: BluetoothRemoteGATTCharacteristic }> {
  try {
    return await connectAndDiscover(device, onStatus);
  } catch (err) {
    console.error("[ble] FAIL initial connect/discover, retrying after reconnect:", err);
    await sleep(1500);
    console.log("[ble] -> reconnect before connect/discover retry");
    device.gatt?.disconnect();
    await sleep(300);
    return await connectAndDiscover(device, onStatus);
  }
}

// Must be called from a user gesture (e.g. a button's onClick) — Web
// Bluetooth's requestDevice() throws otherwise.
export async function provisionDevice(
  deviceToken: string,
  onStatus: (status: DeviceStatus) => void,
): Promise<void> {
  if (!isWebBluetoothAvailable()) {
    throw new BleProvisioningError(
      "Web Bluetooth isn't available in this browser. Use Chrome on Android or desktop.",
    );
  }

  let device: BluetoothDevice;
  console.log("[ble] -> requestDevice");
  try {
    device = await navigator.bluetooth!.requestDevice({
      filters: [{ services: [SERVICE_UUID] }],
    });
    console.log("[ble] OK requestDevice", device.name);
  } catch (err) {
    console.error("[ble] FAIL requestDevice:", err);
    throw new BleProvisioningError("No device selected, or the scan was cancelled.");
  }

  let tokenChar: BluetoothRemoteGATTCharacteristic;
  try {
    ({ tokenChar } = await connectAndDiscoverWithRetry(device, onStatus));
  } catch (err) {
    throw new BleProvisioningError(
      `Could not connect to dani-voice: ${describeError(err)}`,
    );
  }

  try {
    const bytes = new TextEncoder().encode(deviceToken);
    console.log(`[ble] token is ${bytes.length} bytes`);
    // Reconnecting drops and re-establishes the GATT connection, which
    // forces NimBLE to free the old connection's prepare-write queue (see
    // withEncryptionRetry's comment) — re-fetching the service/characteristic
    // is required because they're invalidated by the disconnect.
    const reconnect = async () => {
      console.log("[ble] -> reconnect: disconnect");
      device.gatt?.disconnect();
      console.log("[ble] -> reconnect: gatt.connect");
      const reconnected = await device.gatt?.connect();
      if (!reconnected) throw new Error("reconnect failed");
      console.log("[ble] OK reconnect: gatt.connect");
      console.log("[ble] -> reconnect: getPrimaryService");
      const reconnectedService = await reconnected.getPrimaryService(SERVICE_UUID);
      console.log("[ble] OK reconnect: getPrimaryService");
      console.log("[ble] -> reconnect: getCharacteristic (token)");
      tokenChar = await reconnectedService.getCharacteristic(TOKEN_CHARACTERISTIC_UUID);
      console.log("[ble] OK reconnect: getCharacteristic (token)");
    };
    // One outer timeout bounds the whole retry sequence (first attempt +
    // 2s delay + reconnect + second attempt) as a last-resort UI-freeze
    // guard — it does not trigger another retry of its own if it fires (see
    // withEncryptionRetry's comment on why that would be unsafe here).
    await withTimeout("write token (including retry)", 25_000, () =>
      withEncryptionRetry(() => tokenChar.writeValueWithResponse(bytes), reconnect),
    );
  } catch (err) {
    throw new BleProvisioningError(
      `Could not write the token. Wait a few seconds, then disconnect and tap "Provision ESP32" again: ${describeError(err)}`,
    );
  }
}
