// Client for the backend's Deepgram streaming STT proxy
// (backend/src/voice_cowork_backend/routers/stt.py). One connection per
// hold-to-talk press: connect() while the button is held, sendFrame()
// per captured PCM frame (see mic-capture.ts), finalize() on release.
//
// Auth: a raw browser WebSocket can't set an Authorization header, so the
// session token is sent as the first frame after the socket opens
// (`{"type":"auth","token":...}`) rather than a query-string token or WS
// subprotocol — see the approved plan for why. The backend closes with a
// specific code (4401) if auth or the Deepgram connection fails; callers
// should watch onClose to distinguish a clean shutdown from a failure.
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

// Already-accumulated across Deepgram's internal per-segment boundaries
// (see backend/src/voice_cowork_backend/stt_driver.py's
// TranscriptAccumulator) — `text` is the full utterance so far, not one
// segment. This client never sees Deepgram's raw event shape at all.
export interface SttTranscriptEvent {
  type: "transcript";
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
      const ws = new WebSocket(wsStreamUrl("/stt/stream"));
      this.ws = ws;
      let settled = false;

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "auth", token }));
        settled = true;
        resolve();
      };
      ws.onmessage = (event) => {
        try {
          onTranscript(JSON.parse(event.data as string) as SttTranscriptEvent);
        } catch {
          // malformed frame — ignore rather than throw out of the handler
        }
      };
      ws.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new Error("STT WebSocket connection failed."));
        }
      };
      ws.onclose = (event) => {
        this.ws = null;
        onClose?.(event.code);
      };
    });
  }

  sendFrame(frame: Float32Array): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(floatTo16BitPCM(frame));
  }

  // Signals end-of-utterance and resolves once the backend has actually
  // closed the connection — by that point Deepgram's own finalize
  // protocol (CloseStream -> final transcript(s) -> close) has already
  // run on the backend, so any final transcript event has already been
  // delivered via onTranscript before this resolves.
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
