"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { pair, verifySession, PairError } from "@/lib/api";
import { saveSession, loadSession, clearSession } from "@/lib/session";
import { EspProvisioning } from "@/components/esp-provisioning";

type Status = "loading" | "pairing" | "provisioning";

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

export default function Home() {
  const router = useRouter();
  const [status, setStatus] = useState<Status>("loading");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // A restored, still-valid session has nothing left to do on "/" — session
  // info, provisioning access, and disconnect all live in /chat now, and
  // the provisioning-skip step below is only part of *first-time* pairing.
  useEffect(() => {
    let cancelled = false;

    async function restoreSession() {
      const stored = loadSession();
      if (!stored) {
        if (!cancelled) setStatus("pairing");
        return;
      }
      try {
        await verifySession(stored.access_token);
        if (!cancelled) router.push("/chat");
      } catch {
        clearSession();
        if (!cancelled) setStatus("pairing");
      }
    }

    restoreSession();
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const pairResult = await pair(pin);
      saveSession({
        access_token: pairResult.access_token,
        expires_at: pairResult.expires_at,
      });
      setStatus("provisioning");
    } catch (err) {
      const message =
        err instanceof PairError
          ? (ERROR_MESSAGES[err.code] ?? err.message)
          : "Something went wrong — try again.";
      setError(message);
      setPin("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center bg-background px-6 py-12 font-sans">
      <main className="flex w-full max-w-full sm:max-w-lg flex-col items-center gap-8">
        <h1 className="text-4xl font-bold tracking-tight text-foreground">Dani Voice</h1>

        {status === "loading" && (
          <p className="text-lg text-muted-foreground">Checking session…</p>
        )}

        {status === "pairing" && (
          <form onSubmit={handleSubmit} className="flex w-full flex-col gap-5">
            <label className="flex flex-col gap-2 text-base text-muted-foreground">
              Enter the 6-digit PIN shown on your laptop
              <input
                type="text"
                inputMode="numeric"
                pattern="\d*"
                maxLength={6}
                autoFocus
                value={pin}
                onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 6))}
                className="w-full rounded-xl border-2 border-border bg-background px-4 py-4 text-center text-4xl font-mono tracking-[0.4em] text-foreground outline-none focus:border-ring"
                placeholder="000000"
              />
            </label>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <button
              type="submit"
              disabled={pin.length !== 6 || submitting}
              className="w-full rounded-xl bg-primary px-5 py-4 text-lg font-semibold text-primary-foreground disabled:opacity-40"
            >
              {submitting ? "Pairing…" : "Pair"}
            </button>
          </form>
        )}

        {status === "provisioning" && (
          <div className="flex w-full flex-col gap-4 text-base text-muted-foreground">
            <p className="text-2xl font-bold text-foreground">Provision ESP32</p>
            <p className="text-sm">
              Optional — pair an ESP32 device now over Bluetooth, or skip and do this later from
              the chat screen.
            </p>
            <EspProvisioning onDone={() => router.push("/chat")} />
            <button
              onClick={() => router.push("/chat")}
              className="w-full rounded-xl border-2 border-border px-5 py-4 text-lg font-semibold text-foreground"
            >
              Skip
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
