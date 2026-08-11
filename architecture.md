# Dani Voice — Architecture (current state vs. `VOICE_ARCHITECTURE.md`)

> Living summary of how the implemented system actually fits together, and how it
> diverges from the recommendations in `VOICE_ARCHITECTURE.md`. `PROGRESS.md` is
> the authoritative session log; this file is the current-state map.

## 1. System overview

```
Phone (Chrome/PWA)             tunnel            Laptop (host)          AI tool
─────────────────          ─────────────        ──────────────────     ──────────
• /pair PIN/QR auth             HTTPS            FastAPI backend        opencode /
• /device/pair → BLE token                       + named tunnel         dani-cli /
• chat UI + mic + speaker                        + AI server spawn       mimocode
• Web Bluetooth → ESP32       ◄────────────►     • PIN/session/device auth
                                                • opencode relay (SSE)
                                                • permission + question relay
                                                • STT: pywhispercpp full-clip
                                                  + sherpa-onnx streaming WS
                                                • TTS: Kokoro (default) /
                                                  pyttsx3 / Deepgram (chunked SSE)
                                                • Groq tool-call summaries
```

## 2. Backend surface

| Area | Routes |
|---|---|
| Pairing | `POST /pair` (phone), `POST /device/pair` (ESP32 token), `GET /internal/pin` |
| Session | `GET /session/verify`, `POST /session/refresh`, `GET/POST /internal/sessions`, `POST /internal/revoke` |
| AI tool | `POST /opencode/command`, `POST /opencode/command/stream` (SSE), `GET /opencode/permissions`, `POST /opencode/permissions/{id}/reply`, `GET /opencode/questions`, `POST /opencode/questions/{id}/reply`, `POST /opencode/questions/{id}/reject`, `GET /opencode/info`, `POST /opencode/switch-tool` |
| Audio | `POST /audio/transcribe` (full clip → pywhispercpp), `WS /audio/transcribe/stream` (sherpa-onnx partials), `POST /audio/speak/stream` (SSE chunked TTS) |

Internal (`/internal/*`) routes are tunnel-gated (404 when a `cf-connecting-ip`
is present); the CLI reads PIN/sessions locally.

## 3. Audio pipeline — implemented state

### STT — two paths, both local on the host
- **Full-clip:** `POST /audio/transcribe` — browser uploads a recorded blob →
  ffmpeg → 16kHz mono WAV → `pywhispercpp` (`base.en`) → full transcript.
  This is still the browser UI path for recorded clips.
- **Streaming:** `WS /audio/transcribe/stream` — raw 16kHz mono S16LE PCM binary
  frames, auth is the first JSON text frame `{"token": …}`, partials flow as
  `{"type":"partial","text":…}`, `{"type":"stop"}` ends the utterance, then
  `{"type":"final",…}` + close. Implemented with **sherpa-onnx** streaming
  zipformer (int8), not whisper.cpp `--stream` as the doc suggested. A
  `finalize()` silence-tail flush recovers the decoder's last ~1s of in-flight
  hypothesis. Backend verified (live WS: 6 progressive partials, final ~0.32s
  after audio end).

### TTS — three engines, chunked SSE
- Default is **Kokoro-82M local ONNX** (`kokoro-onnx`, 24kHz WAV) — this is the
  biggest divergence from the doc, which recommended flipping to **Deepgram
  Aura-2** as the default. Kokoro gives a natural local voice, no per-call cost,
  no audio leaving the laptop; ~1.7–2.3s warm first-chunk TTFB.
- **pyttsx3** (local SAPI5) and **Deepgram** (cloud, `aura-2-thalia-en`) remain
  switchable via `VC_TTS_ENGINE` with no code change.
- `POST /audio/speak/stream` splits sanitized text into sentence chunks and
  yields each as soon as it's synthesized (base64 `audio/wav` SSE). The
  frontend also receives **inline `audio_chunk` events inside the
  `/opencode/command/stream` SSE** (live per-turn playback, Stage 4), and a
  separate `audio/speak/stream` call powers the per-message "Listen" replay.
- `markdown_to_speech()` strips code blocks, headers, links, etc. before TTS.

## 4. Frontend voice state

- `web/lib/mic-capture.ts` — **AudioWorklet** raw-PCM capture (16kHz mono
  Float32 frames, linear resample) with an `onSpeechChange` callback fed by the
  worklet's energy VAD. Replaces the old MediaRecorder clip model.
- `web/lib/stt-client.ts` — WebSocket client for `/audio/transcribe/stream`
  (sherpa-onnx): auth `{"token":…}` first, `partial`/`final` transcript events.
- `web/public/worklets/pcm-capture-worklet.js` — 20ms frame accumulator +
  energy VAD (adaptive noise floor, hangover window) posting `{frame, speech}`.
- `web/lib/audio-playback-queue.ts` — decoupled chunk-arrival/playback queue
  with replay, interrupt (`stop()` revokes object URLs), pause/resume.
- `web/lib/api.ts` — typed wrappers for pair/device-pair, session,
  command/command-stream SSE, permissions, questions, info/switch-tool,
  `/audio/transcribe`, `/audio/speak/stream`.
- Hold-to-talk / tap-to-toggle gesture handling in `chat/page.tsx` (tap threshold,
  accidental-tap discard, deferred release during setup, mic held open while
  editing composer; open mode auto-stops on ~700ms VAD silence).

## 5. Comparison vs. `VOICE_ARCHITECTURE.md`

| Doc recommendation | Doc priority | Status | Reality |
|---|---|---|---|
| Flip default TTS to Deepgram | #1 | **Diverged** | Default is now **Kokoro local ONNX**; pyttsx3 + Deepgram kept as alternatives. Local neural voice, no cloud cost/privacy cost. |
| Add `/audio/transcribe/stream` WebSocket | #2 | **Built & wired** | sherpa-onnx WS (not whisper.cpp). Frontend `stt-client.ts` now speaks the real protocol (auth `{"token":…}`, `partial`/`final` frames). Verified live. |
| Add VAD on the phone (AudioWorklet energy) | #3 | **Built** | Energy-based VAD in `pcm-capture-worklet.js` (adaptive noise floor + hangover); drives mic button pulse and auto-stops open mode after ~700ms silence. |
| Stream first TTS chunk before response done | #4 | **Done** | Chunked SSE `/audio/speak/stream` + inline `audio_chunk` in command stream; playback starts on chunk 1. |
| Optional: Deepgram live STT | #5 | **Not built** | Rejected in favor of local sherpa-onnx streaming. |
| Optional: Web Speech API fallback | #6 | **Not built** | — |
| Phone-side VAD lives on phone | §2 | **Done** | VAD runs in the AudioWorklet (on-device); host still does the STT/TTS. |
| Push-to-talk or tap-to-toggle shape | §8 | **Done** | Implemented as hold-to-talk with tap-to-toggle; open mode auto-stops on VAD silence. |

### Placement decision (doc's §6 matrix)
The project is effectively **Option A (everything on the host laptop)** plus
streaming STT and a Kokoro upgrade to the TTS-quality hole that Option A had —
it did **not** adopt the doc's recommended Option B (Deepgram for voice).
Rationale implied by the code: keep audio and text local, one-time model
downloads, no per-call cost.

## 6. Known gaps / drift (verified against the tree)

Resolved:
1. **Frontend streaming STT** is now wired to the real backend path.
   `web/lib/stt-client.ts` targets `/audio/transcribe/stream` (sherpa-onnx) with
   the matching auth frame `{"token":…}` and `partial`/`final` events; the stale
   Deepgram `/stt/stream` client is gone. Live voice input works end-to-end.
2. **Phone-side VAD** is in place: energy classification in the AudioWorklet
   (`public/worklets/pcm-capture-worklet.js`), surfaced through
   `MicCapture.onSpeechChange`, driving the mic button's de-press pulse and the
   ~700ms auto-stop in open (tap-to-toggle) mode. Silero (the 8b plan's
   heavier option) remains a future upgrade.

Still open:
3. `camera_streamer_start()` in `firmware/main/main.c` is temporarily commented
   out (diagnostic state; must be restored — see AGENTS.md).
4. Phase 8 firmware: ESP32 direct backend auth over Wi-Fi, the ESP32 audio path
   (8c — needs the ESP-IDF toolchain + hardware; not installable in this
   environment), and 8e latency tuning. 8d CLI wiring (`--tts`/`--stt` flags on
   `voice-cowork run`) is done.
5. Phase 9: the full E2E test now exists (`backend/scripts/e2e_test.py`) and
   passes live (pair → streaming STT → TTS → optional AI command leg); a
   browser-only pass remains the user's part.

## 7. Latency model (from the doc, still accurate)

The tunnel (~30–80ms RTT) is not the bottleneck. Dominant costs:
1. opencode time-to-first-token (~3–10s free-tier models)
2. sentence-end detection before TTS can start
3. TTS first-chunk TTFB (Kokoro ~1.7–2.3s warm on this laptop; pyttsx3 ~0.5s;
   Deepgram ~0.4s)

## 8. Key invariants / gotchas preserved in code

- No containerized backend (needs real filesystem/git access for the AI tool).
- `/audio/transcribe/stream` is a **WebSocket**, not POST+SSE — Starlette can't
  stream a response while reading a streaming request body (verified uvicorn
  0.51).
- Streaming STT model (~296MB tar.bz2) and Kokoro files (~350MB) auto-download
  to `~/.dani/models/…` on first use, warm-loaded at startup.
- Windows: `_import_sherpa_onnx()` registers the venv onnxruntime capi dir via
  `os.add_dll_directory()` before import (stray `C:\WINDOWS\system32` ORT 1.17.1
  shadows venv 1.28); `pyttsx3` gets a fresh engine per call using
  `save_to_file()`.
- Auth: bearer-token session checks for HTTP routes; WS and BLE device paths
  carry their own token handshakes.
