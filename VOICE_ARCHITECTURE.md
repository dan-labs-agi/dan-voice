# Voice Infrastructure Architecture — Dani Voice

> Research & recommendation for where STT, TTS, and VAD should live in the Dani Voice stack.

## TL;DR

| Stage | Default placement | Why |
|---|---|---|
| **Capture (mic)** | **Phone browser** | The mic *is* on the phone. Non-negotiable. |
| **STT** | **Host laptop** (current `pywhispercpp`) | Best accuracy on technical/coding vocabulary, no iOS Safari breakage, already built, no per-call cost. |
| **LLM** | **Host laptop** (opencode) | Already there. Cannot move without rewriting the whole point of the project. |
| **TTS** | **Cloud (Deepgram) for natural voice, host-local (pyttsx3) as offline fallback** | pyttsx3's mechanical SAPI5 voice is genuinely bad for long-form chat; Deepgram's `aura-2` is in a different league for ~$0.00003/char. |
| **Playback** | **Phone browser** | The speaker *is* on the phone. |

The single biggest win you can make right now is **streaming STT** (chunked audio → live partial transcripts while the user is still talking) and **streaming TTS** (already partially done via SSE chunks). Both of these are evolutions of what you have, not a replacement.

---

## 1. The Phone-vs-Laptop Decision, Decomposed

### 1.1 Why STT should stay on the host laptop (for now)

**Browser Web Speech API is unusable for this use case.**

| API | Chrome Android | Safari iOS | Verdict |
|---|---|---|---|
| `SpeechRecognition` (live STT) | ✅ supported, but **fires audio to Google's cloud** (it's not on-device) | ❌ not supported | Privacy surprise + iOS is dead |
| `SpeechSynthesis` (TTS) | ✅ but quality is mediocre | ✅ but voice list is limited | Mixed bag |
| `MediaRecorder` (audio capture) | ✅ Opus/WebM | ✅ Opus/MP4 | Both fine |

The "Web Speech API gives you local STT" claim is a common misconception — on Chrome, `SpeechRecognition` is a thin wrapper around Google's *cloud* STT. You're already paying the network round-trip, you just don't control the model, can't customize it for code vocabulary, and can't fallback to local when offline. On iOS Safari, it doesn't exist.

This means: putting STT "on the phone" via Web Speech API is actually **still cloud STT** — just Google's cloud, with no ability to point at your own backend. Given you already have a tunnel+backend, you control the whole stack better if you push your own audio bytes through your own STT.

**On-device Whisper on phones is real but not ready for production chat.**

Whisper.cpp `tiny` and `base` models run on modern Android (Snapdragon 8 Gen 2/3, Apple A15+) at comfortable real-time factors (~0.3–0.5x on `tiny`, ~0.8–1.2x on `base`). But:

- **First-load cost**: 75MB (`tiny`) to 150MB (`base`) must be downloaded and cached on the phone. Manageable once, but it's a one-time UX cliff.
- **Battery**: a 10-second utterance forces ~3–5 seconds of sustained CPU on the phone. Fine for push-to-talk, punishing for continuous listening.
- **WebAssembly inference** (the only way to run it in a browser without a custom native app) is **2–4× slower** than native C++ inference — so the numbers above double on the same hardware.
- **iOS Safari WebAssembly** has tighter constraints than Android Chrome.

For a *push-to-talk chat UX* (hold-the-button, then release), this would work. For an *always-listening assistant*, it would incinerate battery. Given your current UX is press-to-talk (mic button → record → release → transcribe), the laptop is the right default.

**Your existing setup is already excellent for the technical domain.**

A fast laptop CPU running `pywhispercpp` `base.en` on a 5-second audio clip returns in ~0.5–1s after inference starts. Whisper training on coding vocabulary is, in practice, much better than Google's generic Web Speech recognition at transcribing things like `kubectl get pods -n kube-system` or `pytest -k test_async_stream`. You have full control over the model choice.

### 1.2 Why TTS should move to cloud (Deepgram) with local fallback

You already did this. `VC_TTS_ENGINE=pyttsx3` (default) vs `deepgram` is already a flag flip in `audio_driver.py`. The two providers are distinguishable from your code:

- **`pyttsx3`**: Mechanical SAPI5 voice, free, fully offline, WAV output, but actually sounds like a 2003 screen reader. Fine for command acknowledgement, painful for long-form agent responses.
- **Deepgram Aura-2** (e.g. `aura-2-thalia-en`): Neural, natural sounding, ~$0.00003 per character, sub-second TTFB, supports streaming websocket.

The recommendation given the current code is: **flip the default to `deepgram` for normal use, keep `pyttsx3` as the offline fallback** (already supported). The screenshot-quality demo of voice mode is what makes or breaks the perception of this product. A SAPI5 voice telling you "The tool read the directory contents of the specified file path successfully" loses the room.

### 1.3 Why STT should become *streaming* (the biggest single win)

The current flow is:

```
phone records entire clip → uploads blob → backend ffmpeg → whisper.cpp → returns full transcript
```

Per-turn latency from "release button" to "agent starts thinking" is dominated by whisper.cpp inference (~0.875s for a typical clip per PROGRESS.md) — fine, but the user has felt silent that whole time.

A streaming STT path would look like:

```
phone streams Opus chunks (~250ms each) via WebSocket → backend incremental decode → live partial transcripts back to phone
```

This gives you:

- **Word-by-word transcript appearing in the chat composer** as the user speaks (feels like Apple Dictation)
- **User can release the button and send** as soon as they finish their thought, without waiting for a final transcription pass
- **End-of-utterance detection** can be done by silence (VAD on the laptop side, or on the phone — see §2)

whisper.cpp supports an incremental inference mode via `--step` or `stream` flag; Deepgram Nova has a live WebSocket STT API that does exactly this. The higher-effort version of this is switching STT to streaming-cloud for low latency, with local-whisper fallback for offline.

---

## 2. Where VAD (Voice Activity Detection) Should Live

**On the phone, in the browser**, via `AudioWorklet` + an energy-based or WebRTC VAD.

The reason: the phone already has the audio. If you stream every Opus chunk to the laptop, you burn the user's mobile data on silence. A 30-second push-to-talk utterance is ~30KB of Opus, but a 30-second "I'm thinking" pause with background noise uploaded continuously is 100× that. Phone-side VAD both saves bandwidth and lets the UX feel snappy (button visually de-presses the moment the user stops talking).

This is the one piece that *should* move to the mobile side, not the laptop.

A practical, no-new-dependency implementation:

- `AudioWorklet` → 30ms PCM frames
- Compute frame RMS in the worklet
- Send a `{speech: true/false}` flag + the audio chunk to the main thread
- Main thread holds the mic button visually active until ~700ms of silence AND the audio has dropped below a threshold

For production-grade VAD, **Silero VAD** runs at ~1ms/frame on a modern phone — but it's a 2MB ONNX model, which means another on-device download. Worth it later, not now.

---

## 3. Network Topology Latency

Visible round-trip for a typical push-to-talk exchange on the current stack:

```
phone              tunnel           laptop          opencode
  │ ──HTTPS POST audio blob ──► │ ──localhost──► │ 
  │                              │ ──whisper──►  │ (~1s)
  │ ◄──SSE transcript ◀───────── │ ◀──────────── │
  │ ──HTTPS POST /opencode/command ──► ... (~3s wait for first token)
  │ ◀──SSE deltas + permissions ◀── ... 
  │ ──HTTPS POST permission reply ──► │ ──opencode─┘
  │ ◀──SSE final deltas ◀──────────── │
  │ ──HTTPS POST /audio/speak/stream ──► ... (~0.5s per chunk TTFB)
  │ ◀──SSE audio chunks ───────────── │
  │ (browser plays them sequentially)
```

Cloudflare Tunnel adds ~30–80ms RTT in normal conditions. That is *not* the bottleneck anywhere in this pipeline. The dominant latency is:

1. **opencode's time-to-first-token** (~3–10s on free-tier models, per PROGRESS.md measurement)
2. **Sentence-end detection** for TTS chunking (you need a complete sentence to start synthesizing)
3. **TTFB of the first TTS chunk** (~0.5s local, ~0.4s Deepgram)

Putting STT on the phone does **not** help any of these. Putting TTS on the phone via Web Speech API would help TTS TTFB, but you lose Deepgram's natural voice.

---

## 4. Recommended Architecture

```
┌─────────────────────────────┐                  ┌─────────────────────────────┐
│  PHONE (Chrome / PWA)       │                  │  LAPTOP (host)              │
│  ─────────────────────────  │                  │  ─────────────────────────  │
│  • MediaRecorder → Opus blob│  HTTPS blob       │  /audio/transcribe-stream   │
│  • AudioWorklet VAD          │  upload           │    ├─ ffmpeg → 16k WAV     │
│  • mic permission, button UX │  ══════════════►  │    ├─ whisper-streaming    │
│  • audio playback (<audio>)  │                  │    │  (or Deepgram live)    │
│  • Web Bluetooth for ESP32   │  HTTPS SSE        │  /opencode/command/stream  │
│                             │  ══════════════►  │    ├─ opencode SSE relay   │
│                             │  ◄═════════════   │  /audio/speak/stream       │
│                             │  (SSE chunks +    │    ├─ sentence splitter    │
│                             │   transcript)     │    ├─ Deepgram stream      │
│                             │                  │    └─ pyttsx3 fallback     │
└─────────────────────────────┘                  └─────────────────────────────┘
```

### 4.1 What to build, in priority order

| # | Change | Where | Effort | Impact |
|---|---|---|---|---|
| 1 | **Flip default to `deepgram`** for TTS | `backend/audio_driver.py` (1 line: default in `config.py`) | 5 min | Massive perceived quality jump |
| 2 | **Add `/audio/transcribe/stream` WebSocket** | new router + `whisper.cpp --stream` | 2–3 hours | Live transcript in composer |
| 3 | **Add VAD on the phone** | `web/lib/mic-capture.ts` (already exists) | 1–2 hours | Push-to-talk UX feels tighter |
| 4 | **Stream first TTS chunk before full response** | Already partially done via SSE chunks — verify it actually fires on the first chunk before opencode's `done` event | 1 hour | Reduces perceived LLM→voice latency |
| 5 | **Optional: switch STT to Deepgram live** | New or modified route | 1–2 hours | Cuts STT latency from ~1s to ~300ms, on cloud |
| 6 | **Optional: Web Speech API as a no-network fallback** | Add a `useWebSpeech` flag in `web/lib/mic-capture.ts` | 1 hour | Fallback when laptop is offline |

### 4.2 What NOT to do

- **Don't put STT on the phone via WASM Whisper** until battery + first-load UX is acceptable. The brew is not ready.
- **Don't put TTS on the phone via Web Speech API** unless you're OK with the SAPI5-equivalent voice quality on the phone — and at that point, just use `pyttsx3` on the host.
- **Don't try to bypass the tunnel for STT/TTS** — the tunnel adds ~50ms. Not the bottleneck.

---

## 5. Current Code State (what's already built)

### Backend — `backend/src/voice_cowork_backend/audio_driver.py`

| Function | Engine | Notes |
|---|---|---|
| `transcribe_audio(data)` | `pywhispercpp` (local whisper.cpp) | Takes raw bytes → ffmpeg → 16kHz mono WAV → model.transcribe() → joined text. Lazy singleton model, never blocks event loop. |
| `synthesize_speech_chunks(text)` | `pyttsx3` (local, SAPI5 on Windows) **or** `deepgram` (cloud) | Selectable via `VC_TTS_ENGINE`. Splits text into sentence-level chunks, yields each as it synthesizes. SSE-ready. |
| `markdown_to_speech(text)` | — | Strips markdown (code blocks, bold, links, headers, etc.) before TTS. Lives on backend — any TTS caller gets clean speech for free. |
| `split_into_speech_chunks(text)` | — | Regex-based sentence split, merges chunks shorter than 15 chars. |

### Backend — `backend/src/voice_cowork_backend/routers/audio.py`

| Route | Method | Description |
|---|---|---|
| `/audio/transcribe` | POST | Multipart file upload → `TranscribeResponse { text }` |
| `/audio/speak/stream` | POST | JSON `SpeakRequest { text }` → SSE stream of base64-encoded audio chunks |

### Backend — `backend/config.py` (audio-related)

| Variable | Default | Purpose |
|---|---|---|
| `VC_WHISPER_MODEL` | `base.en` | whisper.cpp model name |
| `VC_TTS_ENGINE` | `pyttsx3` | `pyttsx3` (local/offline) or `deepgram` (cloud) |
| `VC_PYTTSX3_VOICE_ID` | `None` | SAPI5 voice token on Windows |
| `VC_DEEPGRAM_API_KEY` | — | Required only when `VC_TTS_ENGINE=deepgram` |
| `VC_DEEPGRAM_TTS_MODEL` | `aura-2-thalia-en` | Deepgram voice model |

### Frontend — `web/lib/audio-playback-queue.ts`

Sequential audio playback queue. Used by the chat page to play TTS chunks one at a time without overlap.

### Frontend — `web/lib/mic-capture.ts`

`MediaRecorder` wrapper — `start()`/`stop(): Promise<Blob>`, click-to-start/click-to-stop only. No VAD/auto-stop, no live chunked streaming (matches `/audio/transcribe` expecting one complete clip). `isAudioRecordingAvailable()` feature-gate.

### Frontend — `web/app/chat/page.tsx` (audio-related)

- Mic button: click starts recording (red/pulsing icon), click again stops → uploads clip → **appends transcript into the composer's text input** (not auto-send — user reviews/edits and hits Send)
- "Listen" button on each assistant message: fires *once* after the full streamed text has arrived. Calls `speakText()`, caches the returned object URL, plays via `new Audio(url).play()`. No autoplay.

---

## 6. Decision Matrix — All Placement Options

### Option A: Everything on the host laptop (current state)

| Criterion | Score | Notes |
|---|---|---|
| STT accuracy | ✅ Excellent | whisper.cpp base.en, coding vocabulary |
| TTS quality | ⚠️ Mediocre | pyttsx3 SAPI5 — mechanical, robotic |
| Latency (STT) | ⚠️ ~1s end-to-end | ffmpeg + whisper inference, but no network |
| Latency (TTS) | ✅ ~0.5s first chunk | Local synthesis, immediate playback |
| Offline capable | ✅ Yes | No cloud dependency for any voice feature |
| Privacy | ✅ Best | Audio never leaves the laptop |
| Cost | ✅ Free | No per-call charges |
| Scalability | ❌ One laptop | Single-user tool — not a problem for this project |
| Battery (phone) | ✅ Minimal | Just upload one blob at the end |

### Option B: Hybrid — STT on laptop, TTS on Deepgram (recommended)

| Criterion | Score | Notes |
|---|---|---|
| STT accuracy | ✅ Excellent | Same as Option A |
| TTS quality | ✅ Natural | Deepgram Aura-2 neural voice |
| Latency (STT) | ⚠️ ~1s end-to-end | Same as Option A |
| Latency (TTS) | ✅ ~0.4s first chunk | Deepgram REST endpoint |
| Offline capable | ⚠️ Partial | TTS needs network; STT still works offline |
| Privacy | ⚠️ TTS audio leaves device | Text-to-speech bytes go to Deepgram |
| Cost | ⚠️ ~$0.00003/char | Negligible for typical chat volumes |
| Battery (phone) | ✅ Minimal | Same as Option A |

### Option C: Full cloud — STT on Deepgram live, TTS on Deepgram stream

| Criterion | Score | Notes |
|---|---|---|
| STT accuracy | ✅ Good | Deepgram Nova is excellent, especially with diarization |
| STT latency | ✅ ~300ms | Live WebSocket, partials as you speak |
| TTS quality | ✅ Natural | Same as Option B |
| TTS latency | ✅ ~0.4s first chunk | Streaming websocket variant |
| Offline capable | ❌ No | Both STT and TTS need network |
| Privacy | ❌ Worst | Raw audio goes to Deepgram |
| Cost | ⚠️ Higher | Two cloud APIs in flight per exchange |
| Battery (phone) | ✅ Minimal | Same streaming pattern as Option B |

### Option D: STT on phone (WASM Whisper)

| Criterion | Score | Notes |
|---|---|---|
| STT accuracy | ⚠️ Good enough | tiny/base models miss code vocabulary |
| STT latency | ✅ ~real-time | But WASM is 2–4× slower than native |
| Privacy | ✅ Best | Audio never leaves phone |
| Offline capable | ✅ Yes | Fully local inference |
| Battery | ❌ Brutal | Sustained CPU for 3–5s per utterance |
| First load | ❌ 75–150MB download | One-time, but a real UX cliff |
| iOS support | ❌ Poorer | WASM constraints tighter on Safari |

---

## 7. Skills & Tools That Would Apply When Building

| Skill | When to invoke |
|---|---|
| `superpowers:brainstorming` | Before any implementation — lock the UX (single-button vs push-to-talk vs always-on; auto-send or review-first?) |
| `superpowers:test-driven-development` | When building the WebSocket streaming STT route — test contract: "given 250ms of 16kHz PCM, the route emits a partial transcript event within 300ms" |
| `superpowers:systematic-debugging` | Streaming STT bugs (VAD false positives, partial transcripts re-written, end-of-utterance detection). First symptom of any of these should go through this skill, not guess-and-fix |
| `superpowers:verification-before-completion` | TTS swaps — don't trust "Deepgram returned audio bytes." Feed the produced bytes back into `/audio/transcribe` and assert the resulting transcript matches the input |

---

## 8. The One Decision to Make Before Coding

**What's the target voice interaction shape?**

| | **Push-to-talk** (current) | **Tap-to-toggle** | **Always-on** |
|---|---|---|---|
| User holds button, releases to send | Tap once, tap again to send | Mic always listening |
| Battery | Free | Free | Brutal on phone |
| STT placement | Laptop (good) | Laptop (good) | Phone (forced) |
| Fits this project? | ✅ Yes | ✅ Yes | ❌ No |

Given your project is a *remote control* for an AI coding agent — not a Siri-class ambient assistant — **push-to-talk or tap-to-toggle is correct**. That means the laptop stays the right place for STT, and the rest of this recommendation is the right shape.

---

## 9. References

- ESP-IDF VAD integration: https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-reference/system/esp_vad.html
- Whisper.cpp streaming: https://github.com/ggerganov/whisper.cpp/blob/master/examples/stream/stream.cpp
- Deepgram Nova-3 STT API: https://developers.deepgram.com/docs/nova-3
- Deepgram Aura-2 TTS streaming: https://developers.deepgram.com/docs/tts-streaming
- Silero VAD: https://github.com/snakers4/silero-vad
- Web Audio API (AudioWorklet): https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet
- Web Speech API (MDN): https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API
- Web Bluetooth API (MDN): https://developer.mozilla.org/en-US/docs/Web/API/Web_Bluetooth_API
- MediaRecorder API (MDN): https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder_API
- LiveKit Agents: https://docs.livekit.io/agents/
- Pipecat: https://github.com/pipecat-ai/pipecat
