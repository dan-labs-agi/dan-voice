// Client for the backend's streaming STT WebSocket
// (backend/src/voice_cowork_backend/routers/audio.py →
// /audio/transcribe/stream, sherpa-onnx streaming zipformer). One
// connection per hold-to-talk press: connect() while the button is held,
// sendFrame() per captured PCM frame (see mic-capture.ts), finalize() on
// release.
//
// Wire protocol (matches the backend exactly):
//   1. The client sends the session token as the FIRST text frame,
//      `{"token": "…"}`. WebSockets can't set an Authorization header, so
//      this is the auth handshake — it must precede any binary frames.
//   2. The backend replies `{"type":"ready"}` once the token verifies, then
//      accepts raw 16kHz mono S16LE PCM binary frames (~20-160ms each).
//   3. Whenever the running transcript changes, the backend sends
//      `{"type":"partial","text":…}`.
//   4. The client ends the utterance with `{"type":"stop"}`, the backend
//      sends `{"type":"final","text":…}` and closes (code 1000).
//   Auth failure: `{"type":"error","message":…}` + close 4401.
//
// The chat page's mic flow (app/chat/page.tsx) consumes only
// `SttTranscriptEvent.text`, so the external interface (connect/sendFrame/
// finalize/close) is deliberately unchanged from the old Deepgram-proxy
// client this replaces.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL;

function wsStreamUrl(path: string): string {
  if (!API_BASE_URL) throw new Error("NEXT_PUBLIC_API_URL is not configured.");
  return API_BASE_URL.replace(/^http/, "ws") + path;
}

function floatTo16BitPCM(input: Float32Array): ArrayBuffer {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    output[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return output.buffer;
}

export interface SttTranscriptEvent {
  type: "partial" | "final";
  text: string;
  is_final: boolean;
}

export class SttClient {
  private ws: WebSocket | null = null;

  connect(
    token: string,
    onTranscript: (event: SttTranscriptEvent) => void,
    onClose?: (code: number) => void,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(wsStreamUrl("/audio/transcribe/stream"));
      this.ws = ws;
      let settled = false;

      const fail = (err: Error) => {
        if (!settled) {
          settled = true;
          reject(err);
        }
      };

      ws.onopen = () => {
        ws.send(JSON.stringify({ token }));
      };

      ws.onmessage = (event) => {
        let data: { type?: string; text?: string; message?: string };
        try {
          data = JSON.parse(event.data as string);
        } catch {
          return; // malformed frame — ignore rather than throw out of the handler
        }

        if (data.type === "ready") {
          // Auth verified — audio can start flowing. Resolve so the caller
          // kicks off mic capture; any frames sent before this are queued
          // by the socket and processed in order.
          if (!settled) {
            settled = true;
            resolve();
          }
          return;
        }

        if (data.type === "error") {
          // Pre-ready this rejects connect() (the chat shows an error
          // entry); post-ready it's a mid-utterance failure the socket is
          // about to close anyway, so fail() is a no-op and the partials
          // captured so far are the best-effort result.
          fail(new Error(data.message ?? "STT connection failed."));
          return;
        }

        if (data.type === "partial" || data.type === "final") {
          onTranscript({
            type: data.type,
            text: data.text ?? "",
            is_final: data.type === "final",
          });
        }
      };

      ws.onerror = () => fail(new Error("STT WebSocket connection failed."));

      ws.onclose = (event) => {
        this.ws = null;
        onClose?.(event.code);
        fail(new Error(`STT connection closed (${event.code}).`));
      };
    });
  }

  sendFrame(frame: Float32Array): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(floatTo16BitPCM(frame));
  }

  // Signals end-of-utterance and resolves once the backend has actually
  // closed the connection — the `{"type":"final"}` transcript (if any) is
  // delivered via onTranscript before this resolves, since the backend
  // sends it immediately before closing.
  finalize(): Promise<void> {
    return new Promise((resolve) => {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        resolve();
        return;
      }
      ws.addEventListener("close", () => resolve(), { once: true });
      ws.send(JSON.stringify({ type: "stop" }));
    });
  }

  // Hard stop, no finalize handshake — for the accidental-tap/abort path.
  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}

export function isSttClientAvailable(): boolean {
  return typeof WebSocket !== "undefined" && !!API_BASE_URL;
}
