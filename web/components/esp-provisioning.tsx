"use client";

import { useState } from "react";
import { devicePair, PairError } from "@/lib/api";
import { provisionDevice, isWebBluetoothAvailable, BleProvisioningError, type DeviceStatus } from "@/lib/ble";

const ERROR_MESSAGES: Record<string, string> = {
  pin_not_found: "No pairing PIN is active — restart voice-cowork on the laptop.",
  pin_expired: "This PIN expired — restart voice-cowork to get a new one.",
  pin_already_used: "This PIN was already used — restart voice-cowork to get a new one.",
  pin_mismatch: "Incorrect PIN — try again.",
  malformed_pin: "Enter all 6 digits.",
  rate_limited: "Too many attempts — wait a minute and try again.",
  network_error: "Could not reach the backend — check your connection.",
  unknown_error: "Something went wrong — try again.",
};

// Shared between two call sites: inline as a skippable step right after
// first-time pairing (web/app/page.tsx), and inside the re-provisioning
// dialog opened from /chat's header — provisionDevice() itself
// (web/lib/ble.ts) is already a plain, stateless async function with no
// React coupling, so only this presentational piece needed extracting.
export function EspProvisioning({ onDone }: { onDone: () => void }) {
  const [provisionPin, setProvisionPin] = useState("");
  const [provisioning, setProvisioning] = useState(false);
  const [provisionError, setProvisionError] = useState<string | null>(null);
  const [deviceStatus, setDeviceStatus] = useState<DeviceStatus | null>(null);

  async function handleProvision(e: React.FormEvent) {
    e.preventDefault();
    setProvisionError(null);
    setDeviceStatus(null);
    setProvisioning(true);
    try {
      const { access_token } = await devicePair(provisionPin);
      await provisionDevice(access_token, setDeviceStatus);
      onDone();
    } catch (err) {
      const message =
        err instanceof PairError
          ? (ERROR_MESSAGES[err.code] ?? err.message)
          : err instanceof BleProvisioningError
            ? err.message
            : err instanceof Error
              ? `Unexpected error: ${err.message}`
              : `Unexpected error: ${String(err)}`;
      setProvisionError(message);
    } finally {
      setProvisioning(false);
      setProvisionPin("");
    }
  }

  if (!isWebBluetoothAvailable()) {
    return (
      <p className="text-sm text-destructive">
        Web Bluetooth isn&apos;t available in this browser. Use Chrome on Android or desktop.
      </p>
    );
  }

  return (
    <form onSubmit={handleProvision} className="flex w-full flex-col gap-4">
      <label className="flex flex-col gap-2 text-base text-muted-foreground">
        Enter the PIN again to provision the ESP32 over BLE
        <input
          type="text"
          inputMode="numeric"
          pattern="\d*"
          maxLength={6}
          value={provisionPin}
          onChange={(e) => setProvisionPin(e.target.value.replace(/\D/g, "").slice(0, 6))}
          className="w-full rounded-xl border-2 border-border bg-background px-4 py-4 text-center text-3xl font-mono tracking-[0.4em] text-foreground outline-none focus:border-ring"
          placeholder="000000"
        />
      </label>
      {provisionError && <p className="text-sm text-destructive">{provisionError}</p>}
      {deviceStatus && (
        <p className="text-sm text-muted-foreground">
          Device status: <span className="font-semibold">{deviceStatus}</span>
        </p>
      )}
      <button
        type="submit"
        disabled={provisionPin.length !== 6 || provisioning}
        className="w-full rounded-xl bg-primary px-5 py-4 text-lg font-semibold text-primary-foreground disabled:opacity-40"
      >
        {provisioning ? "Provisioning…" : "Provision ESP32"}
      </button>
    </form>
  );
}
