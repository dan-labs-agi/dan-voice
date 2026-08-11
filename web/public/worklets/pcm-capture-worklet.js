// Runs in the AudioWorkletGlobalScope, loaded via
// audioContext.audioWorklet.addModule("/worklets/pcm-capture-worklet.js")
// — plain script, not bundled through Next's module graph, hence living
// under public/ rather than lib/. Accumulates the 128-sample render
// quanta the Web Audio API hands `process()` into ~20ms frames (at
// whatever the AudioContext's real sample rate turns out to be — not
// assumed) and posts each accumulated frame to the main thread as a
// transferable Float32Array buffer (zero-copy), alongside an energy-based
// voice-activity flag (see _classifyFrame).
//
// VAD lives here (not the main thread) so every frame — including the
// ones dropped by the main thread's resampler — is classified, and so the
// speech state never stalls behind a busy UI thread. It's deliberately
// energy-based (no model download, ~free per frame); the 8b plan's Silero
// upgrade would slot in as a heavier classifier here without touching the
// rest of the pipeline.
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._frameSamples = Math.round(sampleRate * 0.02);
    this._buffer = new Float32Array(this._frameSamples);
    this._writeIndex = 0;
    // Energy-VAD state — see _classifyFrame.
    this._noiseFloor = 0;
    this._silentFrames = 0;
    this._speech = false;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channelData = input[0];
    if (!channelData || channelData.length === 0) return true;

    let readIndex = 0;
    while (readIndex < channelData.length) {
      const remainingInFrame = this._frameSamples - this._writeIndex;
      const remainingInInput = channelData.length - readIndex;
      const toCopy = Math.min(remainingInFrame, remainingInInput);
      this._buffer.set(channelData.subarray(readIndex, readIndex + toCopy), this._writeIndex);
      this._writeIndex += toCopy;
      readIndex += toCopy;

      if (this._writeIndex >= this._frameSamples) {
        const speech = this._classifyFrame(this._buffer);
        this.port.postMessage({ frame: this._buffer, speech }, [this._buffer.buffer]);
        this._buffer = new Float32Array(this._frameSamples);
        this._writeIndex = 0;
      }
    }
    return true;
  }

  // Energy-based VAD with an adaptive noise floor and a hangover window.
  //
  // A frame is "speech" when its RMS clears both an absolute floor
  // (~-40dBFS, so dead mics / silence stay silent) and a multiple of the
  // running noise floor (so a loud room isn't declared speech just for
  // having a high ambient level). The noise floor only adapts while
  // confident we're silent. Once speech is detected it is held for a
  // SILENCE_HANGOVER_FRAMES run of quiet frames before flipping back —
  // short gaps inside words/utterances (stops, breath pauses) don't
  // cause flutter.
  _classifyFrame(frame) {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) {
      const v = frame[i];
      sum += v * v;
    }
    const rms = Math.sqrt(sum / frame.length);
    const threshold = Math.max(this._noiseFloor * 2.0, 0.01);

    if (rms >= threshold) {
      this._speech = true;
      this._silentFrames = 0;
    } else {
      this._silentFrames += 1;
      if (this._silentFrames >= 6) {
        this._speech = false;
      }
      // Adapt the noise floor only while actually silent (the hangover
      // window returns early above, so _speech stays true there and the
      // floor is frozen through word gaps).
      if (!this._speech) {
        if (this._noiseFloor === 0) {
          this._noiseFloor = rms;
        } else {
          this._noiseFloor = this._noiseFloor * 0.9 + rms * 0.1;
        }
      }
    }
    return this._speech;
  }
}

registerProcessor("pcm-capture-processor", PCMCaptureProcessor);
